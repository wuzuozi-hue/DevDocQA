#include "docqa/server.hpp"
#include <drogon/drogon.h>
#include <drogon/MultiPart.h>
#include <drogon/nosql/RedisClient.h>
#include <drogon/orm/DbClient.h>
#include <openssl/evp.h>
#include <openssl/crypto.h>
#include <trantor/net/Resolver.h>
#include <chrono>
#include <cctype>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <thread>

namespace docqa {
namespace {
using Clock = std::chrono::steady_clock;
using Result = drogon::orm::Result;
using Request = drogon::HttpRequestPtr;
using Callback = std::function<void(const drogon::HttpResponsePtr&)>;

std::string compact(const Json::Value& value) {
  Json::StreamWriterBuilder writer; writer["indentation"] = "";
  return Json::writeString(writer, value);
}

Json::Value parse(const std::string& text) {
  Json::CharReaderBuilder builder;
  builder["rejectDupKeys"] = true; builder["failIfExtra"] = true;
  auto reader = std::unique_ptr<Json::CharReader>(builder.newCharReader());
  Json::Value result; std::string errors;
  if (!reader->parse(text.data(), text.data() + text.size(), &result, &errors)) return {};
  return result;
}

std::string sha256(std::string_view input) {
  unsigned char digest[EVP_MAX_MD_SIZE]; unsigned int length = 0;
  if (EVP_Digest(input.data(), input.size(), digest, &length, EVP_sha256(), nullptr) != 1)
    throw std::runtime_error("SHA256 failed");
  std::ostringstream out;
  for (unsigned int i = 0; i < length; ++i) out << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(digest[i]);
  return out.str();
}

std::string uuid() { return drogon::utils::getUuid(); }

struct Reply {
  Callback callback;
  std::string id{uuid()};
  explicit Reply(Callback cb) : callback(std::move(cb)) {}
  void send(int code, Json::Value body) const {
    auto response = drogon::HttpResponse::newHttpJsonResponse(body);
    response->setStatusCode(static_cast<drogon::HttpStatusCode>(code));
    response->addHeader("X-Request-Id", id);
    response->addHeader("Cache-Control", "no-store");
    response->addHeader("X-Content-Type-Options", "nosniff");
    callback(response);
  }
  void error(int status, const std::string& code, const std::string& message) const {
    Json::Value body; body["error"]["code"] = code; body["error"]["message"] = message; body["request_id"] = id;
    send(status, std::move(body));
  }
};

class DiskExecutor {
 public:
  DiskExecutor() : thread_([this] { run(); }) {}
  ~DiskExecutor() {
    { std::lock_guard<std::mutex> lock(mutex_); stopped_ = true; }
    cv_.notify_one(); thread_.join();
  }
  void add(std::function<void()> task) {
    { std::lock_guard<std::mutex> lock(mutex_); tasks_.push_back(std::move(task)); }
    cv_.notify_one();
  }
 private:
  void run() {
    for (;;) {
      std::function<void()> task;
      { std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return stopped_ || !tasks_.empty(); });
        if (tasks_.empty() && stopped_) return;
        task = std::move(tasks_.front()); tasks_.pop_front(); }
      try { task(); } catch (...) { LOG_ERROR << "Unhandled disk task failure"; }
    }
  }
  std::mutex mutex_; std::condition_variable cv_; std::deque<std::function<void()>> tasks_;
  bool stopped_{false}; std::thread thread_;
};

struct Upload {
  std::string name, content, digest, extension;
  int errorStatus{0}; std::string errorCode, errorMessage;
};

Upload readUpload(const Request& request) {
  Upload upload;
  auto error = [&](int status, const std::string& code, const std::string& message) {
    upload.errorStatus = status; upload.errorCode = code; upload.errorMessage = message; return upload;
  };
  drogon::MultiPartParser parser;
  if (parser.parse(request) != 0 || parser.getFiles().size() != 1 || parser.getFiles().front().getItemName() != "file")
    return error(400, "invalid_upload", "Upload exactly one multipart field named file");
  const auto& file = parser.getFiles().front(); upload.name = file.getFileName();
  if (upload.name.empty() || upload.name.size() > 255 || !validUtf8(upload.name) ||
      upload.name.find_first_of("/\\\r\n") != std::string::npos || upload.name.find('\0') != std::string::npos)
    return error(400, "invalid_filename", "Use a simple UTF-8 filename");
  const auto dot = upload.name.find_last_of('.');
  upload.extension = dot == std::string::npos ? "" : upload.name.substr(dot);
  std::transform(upload.extension.begin(), upload.extension.end(), upload.extension.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  if (upload.extension != ".md" && upload.extension != ".txt")
    return error(415, "unsupported_format", "Only UTF-8 .md and .txt files are supported");
  if (file.fileLength() > 2 * 1024 * 1024) return error(413, "file_too_large", "File exceeds 2 MiB");
  const auto text = file.fileContent();
  if (text.empty() || blank(text)) return error(400, "empty_document", "Document must contain text");
  if (!validUtf8(text) || text.find('\0') != std::string_view::npos)
    return error(400, "invalid_utf8", "Document must contain valid UTF-8 text without NUL bytes");
  upload.content.assign(text); upload.digest = sha256(text);
  return upload;
}

bool equalSecret(const std::string& value, const std::string& secret) {
  return value.size() == secret.size() && CRYPTO_memcmp(value.data(), secret.data(), secret.size()) == 0;
}
}  // namespace

class Server::Impl : public std::enable_shared_from_this<Server::Impl> {
 public:
  explicit Impl(Config config)
      : config_(std::move(config)),
        questions_("question", config_.concurrency, config_.queueCapacity),
        ingestions_("ingest", config_.ingestConcurrency, config_.ingestQueueCapacity),
        loop_(drogon::app().getLoop()) {
    config_.dataDir = std::filesystem::absolute(config_.dataDir).lexically_normal().string();
    std::filesystem::create_directories(config_.dataDir);
  }

  void post(std::function<void()> task) { loop_->queueInLoop(std::move(task)); }

  template <typename... Args>
  void sql(const std::string& statement, std::function<void(const Result&)> done,
           std::function<void()> fail, Args&&... args) {
    auto self = shared_from_this();
    db_->execSqlAsync(statement,
      [self, done = std::move(done)](const Result& result) {
        self->post([self, done, result] { done(result); });
      },
      [self, fail = std::move(fail)](const std::exception_ptr&) {
        self->post([self, fail] { LOG_WARN << "Database operation failed"; fail(); });
      }, std::forward<Args>(args)...);
  }

  void registerRoutes() {
    auto self = shared_from_this();
    drogon::app().setCustomErrorHandler([](drogon::HttpStatusCode status, const Request&) {
      Json::Value body; body["error"]["code"] = status == drogon::k413RequestEntityTooLarge ? "file_too_large" : "http_error";
      body["error"]["message"] = "Request could not be handled"; body["request_id"] = uuid();
      auto response = drogon::HttpResponse::newHttpJsonResponse(body); response->setStatusCode(status); return response;
    });
    drogon::app().registerHandler("/health", [self](const Request&, Callback&& cb) {
      Reply reply(std::move(cb)); self->post([self, reply] {
        Json::Value body; body["status"] = "ok";
        //body的类型是json.cppgai
        body["ready"] = self->ready_;
        body["service"] = "devdocqa";
        body["active_jobs"] = static_cast<Json::UInt64>(self->questions_.active + self->ingestions_.active);
        body["queued_jobs"] = static_cast<Json::UInt64>(self->questions_.jobs.size() + self->ingestions_.jobs.size());
        for (const auto* queue : {&self->questions_, &self->ingestions_}) {
          auto& counts = body["scheduler"][queue->kind];
          counts["active"] = static_cast<Json::UInt64>(queue->active);
          counts["queued"] = static_cast<Json::UInt64>(queue->jobs.size());
          counts["reserved"] = static_cast<Json::UInt64>(queue->slots.used());
        }
        reply.send(200, body);
      });
    }, {drogon::Get});
    drogon::app().registerHandler("/ready", [self](const Request&, Callback&& cb) {
      Reply reply(std::move(cb)); self->post([self, reply] { self->ready(reply); });
    }, {drogon::Get});
    drogon::app().registerHandler("/v1/documents", [self](const Request& req, Callback&& cb) {
      Reply reply(std::move(cb)); self->post([self, req, reply] {
        if (self->authorized(req, reply)) self->upload(req, reply);
      });
    }, {drogon::Post});
    drogon::app().registerHandler("/v1/documents", [self](const Request& req, Callback&& cb) {
      Reply reply(std::move(cb)); self->post([self, req, reply] {
        if (self->authorized(req, reply)) self->documents(reply);
      });
    }, {drogon::Get});
    drogon::app().registerHandler("/v1/questions", [self](const Request& req, Callback&& cb) {
      Reply reply(std::move(cb)); self->post([self, req, reply] {
        if (self->authorized(req, reply)) self->question(req, reply);
      });
    }, {drogon::Post});
    drogon::app().registerHandler("/v1/jobs/{1}", [self](const Request& req, Callback&& cb, std::string id) {
      Reply reply(std::move(cb)); self->post([self, req, reply, id] {
        if (self->authorized(req, reply)) self->job(id, reply);
      });
    }, {drogon::Get});
  }

  void start() {
    db_ = drogon::orm::DbClient::newPgClient(config_.pgDsn, 4); db_->setTimeout(5);
    auto self = shared_from_this();
    drogon::app().getResolver()->resolve(config_.redisHost, [self](const trantor::InetAddress& address) {
      self->post([self, address] {
        if (address.isUnspecified()) { LOG_ERROR << "Redis hostname resolution failed"; return; }
        self->redis_ = drogon::nosql::RedisClient::newRedisClient(
          trantor::InetAddress(address.toIp(), static_cast<std::uint16_t>(self->config_.redisPort), address.isIpV6()), 2);
        self->redis_->setTimeout(2);
      });
    });
    bootstrap();
    loop_->runEvery(2, [self] { if (self->ready_) self->refill(); });
  }

 private:
  struct Job {
    std::string id, kind, cacheKey;
    Json::Value payload;
    Clock::time_point deadline;
    bool running{false}, finishing{false};
    unsigned persistFailures{0};
  };
  using JobPtr = std::shared_ptr<Job>;

  struct JobQueue {
    JobQueue(std::string name, std::size_t workers, std::size_t waiting)
        : kind(std::move(name)), concurrency(workers), slots(workers, waiting) {}

    std::string kind;
    std::size_t concurrency;
    Capacity slots;
    std::deque<JobPtr> jobs;
    std::size_t active{0};
    bool refilling{false};
  };

  JobQueue& queueFor(const Job& task) {
    return task.kind == "ingest" ? ingestions_ : questions_;
  }

  bool authorized(const Request& request, const Reply& reply) {
    if (!equalSecret(request->getHeader("X-API-Key"), config_.apiKey)) {
      reply.error(401, "unauthorized", "Supply a valid X-API-Key"); return false;
    }
    if (!ready_) { reply.error(503, "initializing", "Backend is initializing or recovering"); return false; }
    return true;
  }
  void unavailable(const Reply& reply) { reply.error(503, "database_unavailable", "Database is temporarily unavailable"); }

  void bootstrap() {
    auto self = shared_from_this();
    // CTEs form one atomic statement. A crashed running ingestion can never be
    // left searchable; queued jobs are preserved for FIFO recovery.
    sql("WITH failed AS (UPDATE jobs SET status='failed', error='backend_restarted', finished_at=now() WHERE status='running' RETURNING id), "
        "docs AS (UPDATE documents SET status='failed',error='backend_restarted' WHERE status='processing' AND (SELECT count(*) FROM failed)>=0 RETURNING id), "
        "clean AS (DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE status<>'ready') AND (SELECT count(*) FROM docs)>=0 RETURNING id) "
        "SELECT 1 FROM collection_state WHERE id=1",
      [self](const Result& result) {
        if (result.empty()) { LOG_ERROR << "Database migration is missing"; self->retryBootstrap(); return; }
        self->ready_ = true; self->refill();
      }, [self] { self->retryBootstrap(); });
  }
  void retryBootstrap() {
    auto self = shared_from_this(); loop_->runAfter(3, [self] { self->bootstrap(); });
  }

  void ready(const Reply& reply) {
    if (!ready_ || !redis_) { reply.error(503, "not_ready", "Dependencies are not ready"); return; }
    auto self = shared_from_this();
    sql("SELECT 1", [self, reply](const Result&) {
      self->redis_->execCommandAsync([self, reply](const drogon::nosql::RedisResult&) {
        self->post([self, reply] {
          auto request = drogon::HttpRequest::newHttpRequest(); request->setPath("/health");
          auto client = drogon::HttpClient::newHttpClient(self->config_.workerUrl, self->loop_);
          client->sendRequest(request, [self, reply, client](drogon::ReqResult status, const drogon::HttpResponsePtr& response) {
            self->post([reply, status, response] {
              if (status != drogon::ReqResult::Ok || !response || response->statusCode() != drogon::k200OK) {
                reply.error(503, "worker_unavailable", "AI worker is unavailable"); return;
              }
              Json::Value body; body["status"] = "ready"; reply.send(200, body);
            });
          }, 2);
        });
      }, [self, reply](const std::exception&) {
        self->post([reply] { reply.error(503, "redis_unavailable", "Redis is unavailable"); });
      }, "PING");
    }, [self, reply] { self->unavailable(reply); });
  }

  void documents(const Reply& reply) {
    auto self = shared_from_this();
    sql("SELECT id,name,status,error,created_at::text FROM documents ORDER BY created_at DESC,id",
      [reply](const Result& rows) {
        Json::Value body; body["documents"] = Json::Value(Json::arrayValue);
        for (const auto& row : rows) {
          Json::Value document;
          for (const auto* field : {"id", "name", "status", "error", "created_at"}) document[field] = row[field].as<std::string>();
          body["documents"].append(document);
        }
        reply.send(200, body);
      }, [self, reply] { self->unavailable(reply); });
  }

  void job(const std::string& id, const Reply& reply) {
    if (id.size() > 128) { reply.error(404, "job_not_found", "Job does not exist"); return; }
    auto self = shared_from_this();
    sql("SELECT id,kind,status,result::text,error,created_at::text,started_at::text,finished_at::text FROM jobs WHERE id=$1",
      [reply](const Result& rows) {
        if (rows.empty()) { reply.error(404, "job_not_found", "Job does not exist"); return; }
        const auto& row = rows[0]; Json::Value body;
        body["job_id"] = row["id"].as<std::string>();
        for (const auto* field : {"kind", "status", "error", "created_at"}) body[field] = row[field].as<std::string>();
        for (const auto* field : {"started_at", "finished_at"}) body[field] = row[field].isNull() ? Json::Value() : Json::Value(row[field].as<std::string>());
        body["result"] = row["result"].isNull() ? Json::Value() : parse(row["result"].as<std::string>());
        reply.send(200, body);
      }, [self, reply] { self->unavailable(reply); }, id);
  }

  void upload(const Request& request, const Reply& reply) {
    if (!ingestions_.slots.reserve()) { reply.error(503, "queue_full", "Ingestion capacity is full; retry later"); return; }
    auto self = shared_from_this();
    disk_.add([self, request, reply] {
      Upload upload;
      try { upload = readUpload(request); }
      catch (...) { upload.errorStatus = 500; upload.errorCode = "upload_failed"; upload.errorMessage = "Could not process upload"; }
      self->post([self, reply, upload = std::move(upload)]() mutable {
        if (upload.errorStatus) { self->ingestions_.slots.release(); reply.error(upload.errorStatus, upload.errorCode, upload.errorMessage); return; }
        if (!self->uploadDigests_.insert(upload.digest).second) {
          // Same-content requests share the first durable ingestion submission.
          // Their reservations bound the number of waiting HTTP callbacks.
          self->duplicateWaiters_[upload.digest].push_back(reply); return;
        }
        self->lookupUpload(std::make_shared<Upload>(std::move(upload)), reply);
      });
    });
  }

  void notifyDuplicates(const std::string& digest, const Json::Value* body = nullptr) {
    auto it = duplicateWaiters_.find(digest);
    if (it == duplicateWaiters_.end()) return;
    for (const auto& reply : it->second) {
      ingestions_.slots.release();
      if (body) { auto duplicate = *body; duplicate["duplicate"] = true; reply.send(202, duplicate); }
      else reply.error(503, "upload_failed", "The shared upload failed; retry later");
    }
    duplicateWaiters_.erase(it);
  }

  void releaseUpload(const std::shared_ptr<Upload>& upload, const Json::Value* body = nullptr) {
    uploadDigests_.erase(upload->digest); ingestions_.slots.release(); notifyDuplicates(upload->digest, body);
  }

  void lookupUpload(const std::shared_ptr<Upload>& upload, const Reply& reply) {
    auto self = shared_from_this();
    sql("SELECT d.id,d.name,d.status,d.embedding_signature,(SELECT j.id FROM jobs j WHERE kind='ingest' AND payload->>'document_id'=d.id ORDER BY created_at DESC,id DESC LIMIT 1) AS job_id "
        "FROM documents d WHERE sha256=$1",
      [self, upload, reply](const Result& rows) {
        const bool needsReindex = !rows.empty() && rows[0]["status"].as<std::string>() == "ready" &&
          rows[0]["embedding_signature"].as<std::string>() != self->config_.embeddingSignature;
        if (!rows.empty() && rows[0]["status"].as<std::string>() != "failed" && !needsReindex) {
          Json::Value body; body["document_id"] = rows[0]["id"].as<std::string>();
          body["job_id"] = rows[0]["job_id"].isNull() ? Json::Value() : Json::Value(rows[0]["job_id"].as<std::string>());
          body["duplicate"] = true; self->releaseUpload(upload, &body); reply.send(202, body); return;
        }
        const bool existing = !rows.empty();
        const auto documentId = existing ? rows[0]["id"].as<std::string>() : uuid();
        if (existing) upload->name = rows[0]["name"].as<std::string>();
        self->saveUpload(upload, reply, documentId, existing);
      }, [self, upload, reply] { self->releaseUpload(upload); self->unavailable(reply); }, upload->digest);
  }

  void saveUpload(const std::shared_ptr<Upload>& upload, const Reply& reply, const std::string& documentId, bool existing) {
    const auto path = (std::filesystem::path(config_.dataDir) / (documentId + upload->extension)).string();
    auto self = shared_from_this();
    disk_.add([self, upload, reply, documentId, existing, path] {
      bool saved = false;
      try {
        const auto temporary = path + ".tmp";
        { std::ofstream stream(temporary, std::ios::binary | std::ios::trunc); stream.write(upload->content.data(), static_cast<std::streamsize>(upload->content.size()));
          stream.flush(); if (!stream) throw std::runtime_error("write failed"); }
        std::error_code ec; std::filesystem::remove(path, ec);
        std::filesystem::rename(temporary, path); saved = true;
      } catch (...) { std::error_code ec; std::filesystem::remove(path + ".tmp", ec); }
      self->post([self, upload, reply, documentId, existing, path, saved] {
        if (!saved) { self->releaseUpload(upload); reply.error(500, "storage_failed", "Could not save document"); return; }
        auto task = std::make_shared<Job>(); task->id = uuid(); task->kind = "ingest";
        task->payload["job_id"] = task->id; task->payload["document_id"] = documentId;
        task->payload["path"] = path; task->payload["name"] = upload->name; task->payload["sha256"] = upload->digest;
        task->deadline = Clock::now() + std::chrono::seconds(self->config_.timeout);
        const auto statement = existing
          ? "WITH d AS (UPDATE documents SET status='queued',error='',embedding_signature='' WHERE id=$1 AND status IN ('failed','ready') RETURNING id) INSERT INTO jobs(id,kind,status,payload) SELECT $2,'ingest','queued',$3::jsonb FROM d RETURNING id"
          : "WITH d AS (INSERT INTO documents(id,name,sha256,status) VALUES($1,$4,$5,'queued') RETURNING id) INSERT INTO jobs(id,kind,status,payload) SELECT $2,'ingest','queued',$3::jsonb FROM d RETURNING id";
        self->pendingIds_.insert(task->id);
        auto completed = [self, upload, reply, documentId, task](const Result& result) {
          self->pendingIds_.erase(task->id);
          self->uploadDigests_.erase(upload->digest);
          if (result.empty()) { self->ingestions_.slots.release(); self->notifyDuplicates(upload->digest); reply.error(409, "document_changed", "Document state changed; retry"); return; }
          self->enqueue(task); Json::Value body; body["document_id"] = documentId; body["job_id"] = task->id;
          body["duplicate"] = false; self->notifyDuplicates(upload->digest, &body); reply.send(202, body);
        };
        auto failed = [self, upload, reply, task] { self->pendingIds_.erase(task->id); self->releaseUpload(upload); self->unavailable(reply); };
        if (existing) self->sql(statement, completed, failed, documentId, task->id, compact(task->payload));
        else self->sql(statement, completed, failed, documentId, task->id, compact(task->payload), upload->name, upload->digest);
      });
    });
  }

  void question(const Request& request, const Reply& reply) {
    if (request->body().size() > 16384) { reply.error(413, "question_too_large", "Question request exceeds 16 KiB"); return; }
    auto body = parse(std::string(request->body()));
    if (!body.isObject() || !body["question"].isString()) { reply.error(400, "invalid_json", "Expected JSON object with a string question"); return; }
    const auto text = body["question"].asString();
    if (text.empty() || text.size() > 4000 || blank(text) || !validUtf8(text) || text.find('\0') != std::string::npos) {
      reply.error(400, "invalid_question", "Question must contain 1–4000 UTF-8 bytes"); return;
    }
    Json::Value requested(Json::arrayValue);
    if (body.isMember("document_ids")) {
      if (!body["document_ids"].isArray() || body["document_ids"].size() > 200) { reply.error(400, "invalid_documents", "document_ids must be an array of at most 200 IDs"); return; }
      std::set<std::string> ids;
      for (const auto& value : body["document_ids"]) {
        if (!value.isString() || value.asString().empty() || value.asString().size() > 128) {
          reply.error(400, "invalid_documents", "Each document ID must be a nonempty string"); return;
        }
        const auto identifier = value.asString();
        if (!std::all_of(identifier.begin(), identifier.end(), [](unsigned char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-'; })) {
          reply.error(400, "invalid_documents", "Document IDs may contain letters, digits, underscores and hyphens"); return;
        }
        ids.insert(value.asString());
      }
      for (const auto& id : ids) requested.append(id);
    }
    if (!redis_) { reply.error(503, "redis_unavailable", "Redis is unavailable; new questions are temporarily disabled"); return; }
    rateLimit(reply, text, requested);
  }

  void rateLimit(const Reply& reply, const std::string& question, const Json::Value& requested) {
    auto self = shared_from_this();
    const auto minute = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count() / 60;
    const auto key = "docqa:rate:" + sha256(config_.apiKey) + ":" + std::to_string(minute);
    // Expiration is installed atomically with the first increment, avoiding
    // immortal counters if the client dies between INCR and EXPIRE.
    const char* script = "local n=redis.call('INCR',KEYS[1]);if n==1 then redis.call('EXPIRE',KEYS[1],120) end;return n";
    redis_->execCommandAsync([self, reply, question, requested](const drogon::nosql::RedisResult& result) {
      const auto count = result.asInteger();
      self->post([self, reply, question, requested, count] {
        if (count > self->config_.rate) { reply.error(429, "rate_limited", "Question rate limit reached; retry next minute"); return; }
        self->resolveDocuments(reply, question, requested);
      });
    }, [self, reply](const std::exception&) {
      self->post([reply] { reply.error(503, "redis_unavailable", "Redis is unavailable; new questions are temporarily disabled"); });
    }, "EVAL %s 1 %s", script, key.c_str());
  }

  void resolveDocuments(const Reply& reply, const std::string& question, const Json::Value& requested) {
    auto self = shared_from_this();
    sql("SELECT d.id,d.status,d.embedding_signature,s.version::text FROM documents d CROSS JOIN collection_state s WHERE s.id=1 AND "
        "(($1::jsonb='[]'::jsonb AND d.status='ready') OR d.id IN (SELECT jsonb_array_elements_text($1::jsonb))) ORDER BY d.id",
      [self, reply, question, requested](const Result& rows) {
        if (rows.empty()) { reply.error(409, "documents_not_ready", "Upload documents and wait until they are ready"); return; }
        if (rows.size() > 200) { reply.error(409, "too_many_documents", "Select at most 200 documents for one question"); return; }
        if (!requested.empty() && rows.size() != requested.size()) { reply.error(409, "documents_not_ready", "One or more requested documents are missing or not ready"); return; }
        Json::Value ids(Json::arrayValue); std::vector<std::string> strings;
        for (const auto& row : rows) {
          if (row["status"].as<std::string>() != "ready") { reply.error(409, "documents_not_ready", "Requested document is not ready"); return; }
          if (row["embedding_signature"].as<std::string>() != self->config_.embeddingSignature) {
            reply.error(409, "embedding_mismatch", "Embedding model changed; re-upload these documents to reindex"); return;
          }
          const auto id = row["id"].as<std::string>(); ids.append(id); strings.push_back(id);
        }
        const auto cacheKey = "docqa:answer:" + sha256(cacheMaterial(question, strings, rows[0]["version"].as<std::string>(), self->config_.modelSignature));
        self->lookupCache(reply, question, ids, cacheKey);
      }, [self, reply] { self->unavailable(reply); }, compact(requested));
  }

  void lookupCache(const Reply& reply, const std::string& question, const Json::Value& ids, const std::string& key) {
    auto self = shared_from_this();
    redis_->execCommandAsync([self, reply, question, ids, key](const drogon::nosql::RedisResult& result) {
      const auto cached = result.isNil() ? std::string() : result.asString();
      self->post([self, reply, question, ids, key, cached] {
        self->submitQuestion(reply, question, ids, key, parse(cached));
      });
    }, [self, reply](const std::exception&) {
      self->post([reply] { reply.error(503, "redis_unavailable", "Redis is unavailable; new questions are temporarily disabled"); });
    }, "GET %s", key.c_str());
  }

  void submitQuestion(const Reply& reply, const std::string& question, const Json::Value& ids,
                      const std::string& cacheKey, const Json::Value& cached) {
    if (!questions_.slots.reserve()) { reply.error(503, "queue_full", "Question capacity is full; retry later"); return; }
    auto self = shared_from_this(); auto task = std::make_shared<Job>();
    task->id = uuid(); task->kind = "question"; task->cacheKey = cacheKey;
    task->payload["job_id"] = task->id; task->payload["question"] = question; task->payload["document_ids"] = ids;
    task->payload["cache_key"] = cacheKey;
    task->deadline = Clock::now() + std::chrono::seconds(config_.timeout);
    const bool hit = cached.isObject() && cached["answer"].isString() && cached["citations"].isArray();
    const auto statement = hit
      ? "INSERT INTO jobs(id,kind,status,payload,result,started_at,finished_at) VALUES($1,'question','succeeded',$2::jsonb,$3::jsonb,now(),now()) RETURNING id"
      : "INSERT INTO jobs(id,kind,status,payload) VALUES($1,'question','queued',$2::jsonb) RETURNING id";
    pendingIds_.insert(task->id);
    auto done = [self, reply, task, hit](const Result&) {
      self->pendingIds_.erase(task->id);
      if (hit) self->questions_.slots.release(); else self->enqueue(task);
      Json::Value body; body["job_id"] = task->id; body["cached"] = hit; reply.send(202, body);
    };
    auto fail = [self, reply, task] { self->pendingIds_.erase(task->id); self->questions_.slots.release(); self->unavailable(reply); };
    if (hit) sql(statement, done, fail, task->id, compact(task->payload), compact(cached));
    else sql(statement, done, fail, task->id, compact(task->payload));
  }

  void enqueue(const JobPtr& task) {
    auto& queue = queueFor(*task);
    if (loaded_.count(task->id)) {
      queue.slots.release(); LOG_WARN << "Duplicate scheduler admission suppressed job=" << task->id; return;
    }
    loaded_.insert(task->id); queue.jobs.push_back(task);
    const auto delay = std::max(0.001, std::chrono::duration<double>(task->deadline - Clock::now()).count());
    auto self = shared_from_this(); loop_->runAfter(delay, [self, task] {
      if (self->loaded_.count(task->id) && !task->finishing) self->finish(task, {}, "job_timeout");
    });
    pump(queue);
  }

  void pump(JobQueue& queue) {
    while (queue.active < queue.concurrency && !queue.jobs.empty()) {
      auto task = queue.jobs.front(); queue.jobs.pop_front();
      if (task->finishing) continue;
      task->running = true; ++queue.active;
      if (Clock::now() >= task->deadline) finish(task, {}, "job_timeout"); else run(task);
    }
  }

  void run(const JobPtr& task) {
    auto self = shared_from_this();
    if (!task->payload.isObject() || (task->kind != "ingest" && task->kind != "question") ||
        (task->kind == "question" && (!task->payload["question"].isString() || task->payload["question"].asString().empty())) ||
        (task->kind == "ingest" && (!task->payload["document_id"].isString() || !task->payload["path"].isString() ||
                                  !task->payload["name"].isString() || !task->payload["sha256"].isString()))) {
      finish(task, {}, "invalid_job_payload"); return;
    }
    const auto statement = task->kind == "ingest"
      ? "WITH j AS (UPDATE jobs SET status='running',started_at=now() WHERE id=$1 AND status='queued' RETURNING id) UPDATE documents SET status='processing',error='' WHERE id=$2 AND status='queued' AND EXISTS(SELECT 1 FROM j) RETURNING id"
      : "UPDATE jobs SET status='running',started_at=now() WHERE id=$1 AND status='queued' RETURNING id";
    auto done = [self, task](const Result& rows) {
      if (task->finishing) return;
      if (rows.empty()) { self->finish(task, {}, "invalid_job_state"); return; }
      if (Clock::now() >= task->deadline) { self->finish(task, {}, "job_timeout"); return; }
      auto workerPayload = task->payload;
      workerPayload.removeMember("cache_key");
      workerPayload["job_id"] = task->id;
      auto request = drogon::HttpRequest::newHttpJsonRequest(workerPayload);
      request->setMethod(drogon::Post); request->setPath(task->kind == "ingest" ? "/internal/ingest" : "/internal/answer");
      request->addHeader("X-Internal-Key", self->config_.internalKey);
      auto client = drogon::HttpClient::newHttpClient(self->config_.workerUrl, self->loop_);
      const auto remaining = std::max(0.001, std::chrono::duration<double>(task->deadline - Clock::now()).count());
      client->sendRequest(request, [self, task, client](drogon::ReqResult status, const drogon::HttpResponsePtr& response) {
        self->post([self, task, status, response] {
          if (task->finishing) return;
          if (status == drogon::ReqResult::Timeout || Clock::now() >= task->deadline) { self->finish(task, {}, "job_timeout"); return; }
          if (status != drogon::ReqResult::Ok || !response) { self->finish(task, {}, "worker_unavailable"); return; }
          if (response->statusCode() != drogon::k200OK) { self->finish(task, {}, "worker_failed"); return; }
          auto value = parse(std::string(response->body()));
          if (!value.isObject() || (task->kind == "question" && (!value["answer"].isString() || !value["citations"].isArray())) ||
              (task->kind == "ingest" && (!value["chunks_count"].isIntegral() || !value["embedding_signature"].isString()))) {
            self->finish(task, {}, "invalid_worker_response"); return;
          }
          if (task->kind == "ingest" && value["embedding_signature"].asString() != self->config_.embeddingSignature) {
            self->finish(task, {}, "embedding_mismatch"); return;
          }
          self->finish(task, value, "");
        });
      }, remaining);
    };
    auto fail = [self, task] { self->finish(task, {}, "database_unavailable"); };
    if (task->kind == "ingest") sql(statement, done, fail, task->id, task->payload["document_id"].asString());
    else sql(statement, done, fail, task->id);
  }

  void finish(const JobPtr& task, const Json::Value& result, const std::string& error) {
    if (task->finishing) return;
    task->finishing = true;
    auto& jobs = queueFor(*task).jobs;
    if (!task->running) jobs.erase(std::remove(jobs.begin(), jobs.end(), task), jobs.end());
    persistFinish(task, result, error);
  }

  void persistFinish(const JobPtr& task, const Json::Value& result, const std::string& error) {
    auto self = shared_from_this();
    auto done = [self, task, result, error](const Result&) {
      LOG_INFO << "job=" << task->id << " kind=" << task->kind << " outcome=" << (error.empty() ? "succeeded" : error);
      if (error.empty() && task->kind == "question" && !task->cacheKey.empty() && self->redis_) {
        const auto encoded = compact(result);
        self->redis_->execCommandAsync([](const drogon::nosql::RedisResult&) {}, [](const std::exception&) {},
          "SET %s %b EX %d", task->cacheKey.c_str(), encoded.data(), encoded.size(), self->config_.cacheTtl);
      }
      auto& queue = self->queueFor(*task);
      if (task->running) --queue.active;
      self->loaded_.erase(task->id); queue.slots.release();
      self->pump(queue); self->refill(queue);
    };
    auto fail = [self, task, result, error] {

      // Keep the reservation until durable finalization succeeds. Otherwise a
      // database outage could admit unbounded work and hide unfinished jobs.
      if (task->persistFailures < 5) {
          ++task->persistFailures;
        }
        const int delays[] = {0, 2, 4, 8, 16, 30};
        const int delay = delays[task->persistFailures];
      LOG_WARN << "job=" << task->id
         << " stage=terminal_persist_retry retry_after_s=" << delay;
      self->loop_->runAfter(delay, [self, task, result, error] { self->persistFinish(task, result, error); });
    };
    if (task->kind == "ingest") {
      const auto documentId = task->payload.isObject() && task->payload["document_id"].isString() ? task->payload["document_id"].asString() : std::string();
      if (error.empty()) {
        sql("WITH j AS (UPDATE jobs SET status='succeeded',result=$2::jsonb,error='',finished_at=now() WHERE id=$1 AND status='running' RETURNING id), "
            "d AS (UPDATE documents SET status='ready',error='',embedding_signature=$4 WHERE id=$3 AND status='processing' AND EXISTS(SELECT 1 FROM j) RETURNING id) "
            "UPDATE collection_state SET version=version+1 WHERE id=1 AND EXISTS(SELECT 1 FROM d) RETURNING version",
          done, fail, task->id, compact(result), documentId, result["embedding_signature"].asString());
      } else {
        sql("WITH j AS (UPDATE jobs SET status='failed',result=NULL,error=$2,finished_at=now() WHERE id=$1 AND status IN ('queued','running') RETURNING id), "
            "d AS (UPDATE documents SET status='failed',error=$2 WHERE id=$3 AND status<>'ready' AND EXISTS(SELECT 1 FROM j) RETURNING id) "
            "DELETE FROM chunks WHERE document_id IN (SELECT id FROM d)", done, fail, task->id, error, documentId);
      }
    } else {
      sql("UPDATE jobs SET status=$2,result=$3::jsonb,error=$4,finished_at=now() WHERE id=$1 AND status IN ('queued','running')",
        done, fail, task->id, error.empty() ? std::string("succeeded") : std::string("failed"),
        error.empty() ? compact(result) : std::string("null"), error);
    }
  }

  void refill() {
    refill(questions_);
    refill(ingestions_);
  }

  void refill(JobQueue& queue) {
    if (queue.refilling || !queue.slots.available()) return;
    queue.refilling = true;
    const auto limit = queue.slots.available();
    Json::Value excluded(Json::arrayValue); for (const auto& id : loaded_) excluded.append(id);
    for (const auto& id : pendingIds_) excluded.append(id);
    auto self = shared_from_this();
    sql("SELECT id,kind,payload::text,EXTRACT(EPOCH FROM(now()-created_at)) AS age FROM jobs WHERE status='queued' AND kind=$3 "
        "AND id NOT IN (SELECT jsonb_array_elements_text($1::jsonb)) ORDER BY created_at,id LIMIT $2::integer",
      [self, &queue](const Result& rows) {
        queue.refilling = false;
        for (const auto& row : rows) {
          const auto id = row["id"].as<std::string>();
          // A submission may acquire its ID after the recovery SELECT was
          // issued. Recheck current state on this serialized loop, not just the
          // excluded-ID snapshot used to construct that SELECT.
          if (self->loaded_.count(id) || self->pendingIds_.count(id)) continue;
          // Submissions may fill slots while the SELECT is in flight. Reserve
          // only for rows we can enqueue; the rest stay durably queued.
          if (!queue.slots.reserve()) break;
          auto task = std::make_shared<Job>(); task->id = id; task->kind = row["kind"].as<std::string>();
          task->payload = parse(row["payload"].as<std::string>());
          if (task->payload.isObject() && task->payload["cache_key"].isString()) task->cacheKey = task->payload["cache_key"].asString();
          const double remaining = self->config_.timeout - row["age"].as<double>();
          task->deadline = Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(remaining));
          self->enqueue(task);
        }
      }, [self, &queue] {
        queue.refilling = false;
        self->loop_->runAfter(2, [self, &queue] { self->refill(queue); });
      }, compact(excluded), static_cast<int>(limit), queue.kind);
  }

  Config config_; JobQueue questions_, ingestions_; trantor::EventLoop* loop_;
  std::shared_ptr<drogon::orm::DbClient> db_;
  std::shared_ptr<drogon::nosql::RedisClient> redis_;
  DiskExecutor disk_;
  bool ready_{false};
  std::set<std::string> loaded_, uploadDigests_, pendingIds_;
  std::map<std::string, std::vector<Reply>> duplicateWaiters_;
};

Server::Server(Config config) : impl_(std::make_shared<Impl>(std::move(config))) {}
Server::~Server() = default;
void Server::registerRoutes() { impl_->registerRoutes(); }
void Server::start() { impl_->start(); }
}  // namespace docqa
