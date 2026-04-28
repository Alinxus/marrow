import Foundation

/// No-op analytics manager for Marrow.
@MainActor
class AnalyticsManager {
  static let shared = AnalyticsManager()
  nonisolated static var isDevBuild: Bool { true }
  private init() {}

  func initialize() {}
  func detectAndReportCrash() {}
  func appLaunched() {}
  func trackDisplayInfo() {}
  func trackFirstLaunchIfNeeded() {}
  func identify() {}
  func reportAllSettingsIfNeeded() {}
  func appBecameActive() {}
  func appResignedActive() {}
  func menuBarOpened() {}
  func menuBarActionClicked(action: String) {}

  func settingToggled(setting: String, enabled: Bool = false) {}
  func launchAtLoginChanged(enabled: Bool, source: String) {}
  func launchAtLoginStatusChecked(enabled: Bool) {}

  func notificationSent(type: String = "") {}
  func notificationSent(notificationId: String = "", title: String = "", assistantId: String = "", surface: String = "") {}
  func notificationClicked(notificationId: String = "", title: String = "", assistantId: String = "", surface: String = "") {}
  func notificationDismissed(notificationId: String = "", title: String = "", assistantId: String = "", surface: String = "", durationSeconds: Double = 0) {}
  func notificationWillPresent(type: String = "") {}
  func notificationDelegateReady() {}
  func notificationRepairTriggered(reason: String = "", count: Int = 0) {}
  func notificationSettingsChecked(granted: Bool) {}

  func conversationCreated(source: String = "", hasDevice: Bool = false) {}
  func conversationDetailOpened(hasDevice: Bool = false) {}
  func conversationReprocessed() {}

  func chatMessageSent(messageLength: Int = 0, source: String = "") {}
  func chatSessionCreated(source: String = "") {}
  func chatSessionDeleted() {}
  func chatCleared() {}
  func chatStarredFilterToggled(starred: Bool = false) {}
  func chatAgentQueryCompleted(durationMs: Double = 0, toolCallCount: Int = 0) {}
  func chatAgentError(error: String = "") {}
  func chatAppSelected(appId: String = "", appName: String = "") {}
  func chatBridgeModeChanged(mode: String = "") {}
  func chatToolCallCompleted(toolName: String = "", durationMs: Double = 0) {}
  func sessionRenamed(source: String = "") {}
  func sessionTitleGenerated(source: String = "") {}
  func messageRated(rating: Int = 0, messageId: String = "") {}

  func memoryExtracted(count: Int = 0, source: String = "") {}
  func memoryDeleted(memoryId: String = "", category: String = "") {}
  func memoryListItemClicked() {}
  func knowledgeGraphBuildStarted(fileCount: Int = 0) {}
  func knowledgeGraphBuildCompleted(nodeCount: Int = 0, edgeCount: Int = 0, durationMs: Double = 0) {}
  func knowledgeGraphBuildFailed(error: String = "") {}

  func appDetailViewed(appId: String = "", appName: String = "") {}
  func appEnabled(appId: String = "", appName: String = "") {}
  func appDisabled(appId: String = "", appName: String = "") {}

  func floatingBarToggled(visible: Bool, source: String = "") {}
  func floatingBarAskOmiOpened(source: String = "") {}
  func floatingBarAskOmiClosed(source: String = "") {}
  func floatingBarQuerySent(messageLength: Int = 0, hasScreenshot: Bool = false) {}
  func floatingBarPTTStarted(mode: String = "") {}
  func floatingBarPTTEnded(durationSeconds: Double = 0, wordCount: Int = 0, mode: String = "") {}

  func monitoringStarted(source: String = "") {}
  func monitoringStopped(reason: String = "") {}
  func transcriptionStarted(source: String = "") {}
  func transcriptionStopped(wordCount: Int = 0) {}
  func recordingError(error: String = "") {}

  func taskAdded(source: String = "") {}
  func taskCompleted(source: String = "") {}
  func taskDeleted() {}
  func taskExtracted(count: Int = 0) {}
  func taskPromoted() {}

  func bluetoothStateChanged(state: String = "", deviceType: String = "") {}
  func insightGenerated(type: String = "", assistantId: String = "") {}
  func distractionDetected(appName: String = "") {}
  func focusAlertShown(appName: String = "") {}
  func focusRestored() {}
  func languageChanged(language: String = "") {}
  func tierChanged(from: Int = 0, to: Int = 0, source: String = "") {}
  func tabChanged(tab: String = "") {}
  func searchQueryEntered(query: String = "", source: String = "") {}
  func shareAction(category: String = "") {}
  func feedbackOpened(source: String = "") {}
  func feedbackSubmitted(feedbackLength: Int = 0) {}
  func settingsPageOpened(page: String = "") {}
  func screenCaptureBrokenDetected(consecutiveFailures: Int = 0) {}
  func screenCaptureResetClicked() {}
  func screenCaptureResetCompleted(success: Bool = true) {}
  func deleteAccountClicked() {}
  func deleteAccountConfirmed() {}
  func deleteAccountCancelled() {}
  func trackSettingsState(settings: [String: Any] = [:]) {}
  func trackStartupTiming(name: String = "", duration: Double = 0) {}
  func rewindScreenshotViewed() {}
  func rewindSearchPerformed(query: String = "") {}
  func rewindTimelineNavigated(source: String = "") {}

  func onboardingStepCompleted(step: String = "", stepIndex: Int = 0) {}
  func onboardingCompleted(durationSeconds: Double = 0) {}
  func onboardingHowDidYouHear(source: String = "") {}
  func onboardingChatMessageDetailed(messageLength: Int = 0) {}
  func onboardingChatToolUsed(tool: String = "") {}
  func initialMessageGenerated(durationMs: Double = 0) {}

  func permissionRequested(permission: String = "") {}
  func permissionGranted(permission: String = "") {}
  func permissionSkipped(permission: String = "") {}

  func updateCheckStarted() {}
  func updateAvailable(version: String = "") {}
  func updateNotFound() {}
  func updateInstalled(version: String = "") {}
  func updateCheckFailed(error: String = "", errorDomain: String = "", errorCode: Int = 0,
    underlyingError: String? = nil, underlyingDomain: String? = nil, underlyingCode: Int? = nil) {}
}
