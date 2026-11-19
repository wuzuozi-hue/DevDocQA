#include "docqa/core.hpp"
#include <iostream>
#include <map>
#include <stdexcept>

void check(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}

int main() {
  try {
    docqa::Capacity slots(1, 2);
    check(slots.reserve() && slots.reserve() && slots.reserve(), "pending admission reserves capacity");
    check(!slots.reserve(), "admission rejects when active + waiting + pending are full");
    slots.release(); check(slots.available() == 1 && slots.reserve(), "completion releases capacity");
    docqa::Capacity noWaiting(1, 0);
    check(noWaiting.reserve() && !noWaiting.reserve(), "zero waiting queue");
    noWaiting.release();
    bool rejected = false;
    try { noWaiting.release(); } catch (const std::logic_error&) { rejected = true; }
    check(rejected, "reservation underflow must be caught");
    check(docqa::validUtf8("hello \xe4\xb8\xad\xe6\x96\x87 \xf0\x9f\x98\x80"), "valid multilingual text");
    for (const auto& bad : {std::string("\xc0\xaf"), std::string("\xed\xa0\x80"), std::string("\xf4\x90\x80\x80"), std::string("\xe4\xb8"), std::string("\x80")})
      check(!docqa::validUtf8(bad), "reject invalid, overlong, surrogate, truncated UTF8");
    check(docqa::blank("\r\n \t") && !docqa::blank("x"), "empty question validation");
    const auto base = docqa::cacheMaterial("ab", {"b", "a", "a"}, "1", "model");
    check(base == docqa::cacheMaterial("ab", {"a", "b"}, "1", "model"), "document set order invariant");
    check(base != docqa::cacheMaterial("a", {"bb", "a"}, "1", "model"), "cache framing is unambiguous");
    check(base != docqa::cacheMaterial("ab", {"a", "b"}, "2", "model"), "collection revision invalidates cache");
    check(base != docqa::cacheMaterial("ab", {"a", "b"}, "1", "another"), "model config invalidates cache");
    std::map<std::string, std::string> env{{"API_KEY", "test-api-key-0123456789"}, {"INTERNAL_KEY", "test-internal-key-0123456789"}};
    auto reader = [&](const std::string& k) { auto it = env.find(k); return it == env.end() ? "" : it->second; };
    auto config = docqa::Config::read(reader);
    check(config.port == 8080 && config.concurrency == 1 && config.queueCapacity == 16 && config.timeout == 120, "stable defaults");
    env["QUEUE_CAPACITY"] = "0"; check(docqa::Config::read(reader).queueCapacity == 0, "zero waiting allowed");
    for (const auto& bad : {"-1", "1x", "999999999999999999999999"}) {
      env["DOCQA_PORT"] = bad; rejected = false;
      try { docqa::Config::read(reader); } catch (const std::invalid_argument&) { rejected = true; }
      check(rejected, "bad configuration fails early");
    }
    std::cout << "core: capacity, UTF8, cache framing, configuration passed\n";
    return 0;
  } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
