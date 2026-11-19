#include "docqa/server.hpp"
#include <drogon/drogon.h>
#include <iostream>

int main() {
  try {
    auto config = docqa::Config::read();
    auto server = std::make_shared<docqa::Server>(config);
    const auto* staticDir = std::getenv("STATIC_DIR");
    const auto* bind = std::getenv("DOCQA_BIND");
    drogon::app().setThreadNum(2)
        .addListener(bind && *bind ? bind : "127.0.0.1", static_cast<std::uint16_t>(config.port))
        .setClientMaxBodySize(2 * 1024 * 1024 + 65536)
        .setClientMaxMemoryBodySize(2 * 1024 * 1024 + 65536)
        .setDocumentRoot(staticDir && *staticDir ? staticDir : "web")
        .setHomePage("index.html")
        .setLogLevel(trantor::Logger::kInfo);
    server->registerRoutes();
    drogon::app().registerBeginningAdvice([server] { server->start(); });
    drogon::app().run();
    return 0;
  } catch (const std::exception& e) {
    // Configuration messages contain variable names only, never variable values.
    std::cerr << "Startup failed: " << e.what() << '\n';
    return 1;
  }
}
