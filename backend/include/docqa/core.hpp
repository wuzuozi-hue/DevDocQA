#pragma once

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace docqa {

// Reservation includes uploads and DB submissions, not just queued jobs. All
// access is serialized on the application's scheduling loop.
class Capacity {
 public:
  Capacity(std::size_t workers, std::size_t waiting)
      : limit_(workers + waiting) {
    if (!workers || limit_ < workers) throw std::invalid_argument("invalid capacity");
  }
  bool reserve() {
    if (used_ == limit_) return false;
    ++used_;
    return true;
  }
  void release() {
    if (!used_) throw std::logic_error("reservation underflow");
    --used_;
  }
  std::size_t used() const { return used_; }
  std::size_t available() const { return limit_ - used_; }
 private:
  std::size_t limit_, used_{0};
};

inline bool validUtf8(std::string_view text) {
  std::size_t i = 0;
  while (i < text.size()) {
    const auto c = static_cast<unsigned char>(text[i++]);
    if (c < 0x80) continue;
    unsigned need;
    std::uint32_t value, minimum;
    if (c >= 0xc2 && c <= 0xdf) { need = 1; value = c & 0x1f; minimum = 0x80; }
    else if (c >= 0xe0 && c <= 0xef) { need = 2; value = c & 0x0f; minimum = 0x800; }
    else if (c >= 0xf0 && c <= 0xf4) { need = 3; value = c & 0x07; minimum = 0x10000; }
    else return false;
    if (text.size() - i < need) return false;
    while (need--) {
      const auto next = static_cast<unsigned char>(text[i++]);
      if ((next & 0xc0) != 0x80) return false;
      value = (value << 6) | (next & 0x3f);
    }
    if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
  }
  return true;
}

inline bool blank(std::string_view s) {
  return std::all_of(s.begin(), s.end(), [](unsigned char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
  });
}

inline void frame(std::string& out, std::string_view value) {
  out += std::to_string(value.size()); out += ':'; out.append(value);
}

// Length framing avoids ambiguous concatenation; sorted IDs give set semantics.
inline std::string cacheMaterial(std::string_view question,
                                 std::vector<std::string> ids,
                                 std::string_view version,
                                 std::string_view model) {
  std::sort(ids.begin(), ids.end());
  ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
  std::string out;
  frame(out, "docqa-cache-v1"); frame(out, question); frame(out, version); frame(out, model);
  frame(out, std::to_string(ids.size()));
  for (const auto& id : ids) frame(out, id);
  return out;
}

inline int number(const std::string& value, const std::string& name, int min, int max) {
  if (value.empty() || !std::all_of(value.begin(), value.end(), [](unsigned char c) { return c >= '0' && c <= '9'; }))
    throw std::invalid_argument(name + " must be an integer");
  try {
    const auto n = std::stoll(value);
    if (n < min || n > max) throw std::out_of_range("range");
    return static_cast<int>(n);
  } catch (const std::exception&) { throw std::invalid_argument(name + " is outside the supported range"); }
}

struct Config {
  std::string pgDsn, redisHost, apiKey, internalKey, workerUrl, dataDir, modelSignature, embeddingSignature;
  int redisPort{6379}, port{8080}, concurrency{1}, queueCapacity{16}, timeout{120}, rate{10}, cacheTtl{600};
  using Reader = std::function<std::string(const std::string&)>;
  static std::string environment(const std::string& name) {
    const auto* p = std::getenv(name.c_str()); return p ? std::string(p) : std::string();
  }
  static Config read(Reader reader = environment) {
    auto get = [&](const std::string& name, const std::string& fallback) {
      auto value = reader(name); return value.empty() ? fallback : value;
    };
    Config c;
    c.pgDsn = get("PG_DSN", "host=127.0.0.1 port=5432 dbname=docqa user=docqa password=docqa");
    c.redisHost = get("REDIS_HOST", "127.0.0.1");
    c.apiKey = reader("API_KEY"); c.internalKey = reader("INTERNAL_KEY");
    if (c.apiKey.size() < 16 || c.internalKey.size() < 16)
      throw std::invalid_argument("API_KEY and INTERNAL_KEY must each contain at least 16 characters");
    c.workerUrl = get("AI_SERVICE_URL", "http://127.0.0.1:8000");
    if (c.workerUrl.rfind("http://", 0) && c.workerUrl.rfind("https://", 0))
      throw std::invalid_argument("AI_SERVICE_URL must be an HTTP(S) origin");
    c.dataDir = get("DATA_DIR", "data/documents");
    c.modelSignature = get("MODEL_SIGNATURE", "mock:featurehash-v1;extractive-v1;chunk-v1");
    c.embeddingSignature = get("EMBEDDING_SIGNATURE", "mock:featurehash-v1");
    c.redisPort = number(get("REDIS_PORT", "6379"), "REDIS_PORT", 1, 65535);
    c.port = number(get("DOCQA_PORT", "8080"), "DOCQA_PORT", 1, 65535);
    c.concurrency = number(get("AI_CONCURRENCY", "1"), "AI_CONCURRENCY", 1, 64);
    c.queueCapacity = number(get("QUEUE_CAPACITY", "16"), "QUEUE_CAPACITY", 0, 10000);
    c.timeout = number(get("JOB_TIMEOUT_SECONDS", "120"), "JOB_TIMEOUT_SECONDS", 1, 3600);
    c.rate = number(get("RATE_LIMIT_PER_MINUTE", "10"), "RATE_LIMIT_PER_MINUTE", 1, 1000000);
    c.cacheTtl = number(get("CACHE_TTL_SECONDS", "600"), "CACHE_TTL_SECONDS", 1, 86400);
    return c;
  }
};
}  // namespace docqa
