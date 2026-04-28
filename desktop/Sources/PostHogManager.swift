import Foundation

@MainActor
class PostHogManager {
  static let shared = PostHogManager()
  private init() {}
  func initialize() {}
  func identify() {}
  func reset() {}
}
