#pragma once
#include "docqa/core.hpp"
#include <memory>

namespace docqa {
class Server {
 public:
  explicit Server(Config config);
  ~Server();
  void registerRoutes();
  void start();
 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};
}
