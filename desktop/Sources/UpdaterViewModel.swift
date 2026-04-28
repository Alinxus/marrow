import Foundation
import SwiftUI

/// Update channel stub — no Sparkle in Marrow.
enum UpdateChannel: String, CaseIterable {
  case stable = "stable"
  case beta = "beta"

  var displayName: String { rawValue.capitalized }
  var description: String { "" }

  static var appDisplayName: String { "Marrow" }
}

/// No-op updater for Marrow.
@MainActor
final class UpdaterViewModel: ObservableObject {
  static let shared = UpdaterViewModel()
  @Published var automaticallyChecksForUpdates = false
  @Published var automaticallyDownloadsUpdates = false
  @Published private(set) var canCheckForUpdates = false
  @Published private(set) var updateSessionInProgress = false
  @Published var updateChannel: UpdateChannel = .stable
  @Published var updateAvailable = false
  @Published var availableVersion = ""
  @Published var latestStableBuildNumber: Int? = nil
  @Published var latestStableVersionString: String? = nil
  @Published var activeChannelLabel = ""

  var lastUpdateCheckDate: Date? { nil }
  var currentVersion: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
  }
  var buildNumber: String {
    Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
  }
  var isDowngradeToStable: Bool { false }
  nonisolated static var isUpdateInProgress: Bool { false }

  private init() {}

  func checkForUpdates() {}
  func checkForUpdatesInBackground() {}
  func checkForUpdatesImmediatelyAfterLaunchIfNeeded() {}
  func applyManagedUpdatePolicy() {}
}
