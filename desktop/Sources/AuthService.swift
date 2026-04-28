import Foundation
import AppKit

extension Notification.Name {
  static let userDidSignOut = Notification.Name("ai.marrow.userDidSignOut")
}

enum AuthError: Error {
  case notSignedIn
  case failed(String)
}

/// Local auth — no cloud required. Generates a stable UUID as the local user token.
@MainActor
class AuthService {
  static let shared = AuthService()

  private var authState: AuthState { AuthState.shared }

  var isSignedIn: Bool {
    get { authState.isSignedIn }
    set { authState.isSignedIn = newValue }
  }
  var isLoading: Bool {
    get { authState.isLoading }
    set { authState.isLoading = newValue }
  }
  var error: String? {
    get { authState.error }
    set { authState.error = newValue }
  }

  private let kLocalToken = "marrow_local_token"
  private let kLocalUserId = "marrow_local_user_id"
  private let kLocalEmail = "marrow_local_email"

  private init() {}

  var displayName: String { "Local User" }

  var localUserId: String {
    if let id = UserDefaults.standard.string(forKey: kLocalUserId), !id.isEmpty { return id }
    let id = UUID().uuidString
    UserDefaults.standard.set(id, forKey: kLocalUserId)
    UserDefaults.standard.set(id, forKey: "auth_userId")
    return id
  }

  var localToken: String {
    if let tok = UserDefaults.standard.string(forKey: kLocalToken), !tok.isEmpty { return tok }
    let tok = UUID().uuidString
    UserDefaults.standard.set(tok, forKey: kLocalToken)
    return tok
  }

  /// Called on first launch — signs in automatically with a local identity.
  func configure() {
    let userId = localUserId
    let email = UserDefaults.standard.string(forKey: kLocalEmail) ?? "local@marrow.ai"
    UserDefaults.standard.set(true, forKey: "auth_isSignedIn")
    UserDefaults.standard.set(email, forKey: "auth_userEmail")
    UserDefaults.standard.set(userId, forKey: "auth_userId")
    authState.update(isSignedIn: true, userEmail: email)
    authState.isRestoringAuth = false
    log("AuthService: Configured local user id=\(userId)")
  }

  /// Returns the bearer token for API requests.
  func getIdToken() async throws -> String {
    return localToken
  }

  /// Compat overload — forceRefresh is a no-op for local auth.
  func getIdToken(forceRefresh: Bool) async throws -> String {
    return localToken
  }

  /// Returns a Bearer Authorization header value.
  func getAuthHeader() async throws -> String {
    return "Bearer \(localToken)"
  }

  func signOut() throws {
    UserDefaults.standard.set(false, forKey: "auth_isSignedIn")
    authState.update(isSignedIn: false)
    NotificationCenter.default.post(name: .userDidSignOut, object: nil)
  }

  func fetchConversations() {
    // Conversations are fetched by ConversationService via APIClient.
  }

  func handleOAuthCallback(url: URL) {
    // No OAuth in local mode.
  }
}
