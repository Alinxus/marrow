import SwiftUI

// MARK: - Launch Mode
enum LaunchMode: String {
  case full = "full"
  case rewind = "rewind"

  static func fromCommandLine() -> LaunchMode {
    for arg in CommandLine.arguments {
      if arg == "--mode=rewind" { return .rewind }
    }
    return .full
  }
}

func shouldSkipOnboarding() -> Bool {
  return CommandLine.arguments.contains("--skip-onboarding")
}

@MainActor
class AuthState: ObservableObject {
  static let shared = AuthState()

  private static let kAuthIsSignedIn = "auth_isSignedIn"
  private static let kAuthUserEmail = "auth_userEmail"
  private static let kAuthUserId = "auth_userId"

  @Published var isSignedIn: Bool
  @Published var isLoading: Bool = false
  @Published var isRestoringAuth: Bool = false
  @Published var error: String?
  @Published var userEmail: String?

  private init() {
    let savedSignedIn = UserDefaults.standard.bool(forKey: Self.kAuthIsSignedIn)
    let savedEmail = UserDefaults.standard.string(forKey: Self.kAuthUserEmail)
    self.isSignedIn = savedSignedIn
    self.userEmail = savedEmail
    self.isRestoringAuth = false
  }

  func update(isSignedIn: Bool, userEmail: String? = nil) {
    self.isSignedIn = isSignedIn
    self.userEmail = userEmail
  }

  var userId: String? {
    UserDefaults.standard.string(forKey: Self.kAuthUserId)
  }
}

@main
struct MarrowApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
  @StateObject private var appState = AppState()
  @StateObject private var authState = AuthState.shared
  @Environment(\.openWindow) private var openWindow

  static let launchMode = LaunchMode.fromCommandLine()

  private var windowTitle: String {
    let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""
    let base = Self.launchMode == .rewind ? "Marrow Rewind" : "Marrow"
    return version.isEmpty ? base : "\(base) v\(version)"
  }

  private var defaultWindowSize: CGSize {
    Self.launchMode == .rewind ? CGSize(width: 1000, height: 700) : CGSize(width: 1200, height: 800)
  }

  var body: some Scene {
    let _ = Self.registerOpenMainWindowHandler(openWindow)

    return Window(windowTitle, id: "main") {
      DesktopHomeView()
        .withFontScaling()
    }
    .windowStyle(.titleBar)
    .defaultSize(width: defaultWindowSize.width, height: defaultWindowSize.height)
    .commands {
      CommandGroup(after: .textFormatting) {
        Button("Increase Font Size") {
          let s = FontScaleSettings.shared
          s.scale = min(2.0, round((s.scale + 0.05) * 20) / 20)
        }
        .keyboardShortcut("+", modifiers: .command)

        Button("Decrease Font Size") {
          let s = FontScaleSettings.shared
          s.scale = max(0.5, round((s.scale - 0.05) * 20) / 20)
        }
        .keyboardShortcut("-", modifiers: .command)

        Button("Reset Font Size") {
          FontScaleSettings.shared.resetToDefault()
        }
        .keyboardShortcut("0", modifiers: .command)

        Divider()

        Button("Reset Window Size") {
          resetWindowToDefaultSize()
        }
      }

      CommandGroup(after: .sidebar) {
        Button("Home") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.dashboard.rawValue])
        }
        .keyboardShortcut("1", modifiers: .command)

        Button("Conversations") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.conversations.rawValue])
        }
        .keyboardShortcut("2", modifiers: .command)

        Button("Memories") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.memories.rawValue])
        }
        .keyboardShortcut("3", modifiers: .command)

        Button("Tasks") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.tasks.rawValue])
        }
        .keyboardShortcut("4", modifiers: .command)

        Button("Rewind") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.rewind.rawValue])
        }
        .keyboardShortcut("5", modifiers: .command)

        Button("Apps") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.apps.rawValue])
        }
        .keyboardShortcut("6", modifiers: .command)

        Divider()

        Button("Settings") {
          NotificationCenter.default.post(name: .navigateToSidebarItem, object: nil,
            userInfo: ["rawValue": SidebarNavItem.settings.rawValue])
        }
        .keyboardShortcut(",", modifiers: .command)
      }
    }
  }

  private static func registerOpenMainWindowHandler(_ openWindow: OpenWindowAction) {
    AppDelegate.openMainWindow = { openWindow(id: "main") }
  }
}

class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
  static var openMainWindow: (() -> Void)?

  private var globalHotkeyMonitor: Any?
  private var localHotkeyMonitor: Any?
  private var windowObservers: [NSObjectProtocol] = []
  private var userDefaultsObserver: NSObjectProtocol?
  private var statusBarItem: NSStatusItem?
  private var screenCaptureSwitch: NSSwitch?
  private var audioRecordingSwitch: NSSwitch?
  private var relaunchOnLoginSuppressedForOnboarding = false

  func applicationDidFinishLaunching(_ notification: Notification) {
    if ViewExporter.shouldExport() {
      ViewExporter.run()
      return
    }

    signal(SIGPIPE, SIG_IGN)

    DesktopAutomationBridge.shared.startIfNeeded()

    log("AppDelegate: applicationDidFinishLaunching (mode: \(MarrowApp.launchMode.rawValue))")

    if let iconURL = Bundle.resourceBundle.url(forResource: "omi_app_icon", withExtension: "png"),
      let icon = NSImage(contentsOf: iconURL)
    {
      let size = icon.size
      let maskedIcon = NSImage(size: size)
      maskedIcon.lockFocus()
      let margin = size.width * 0.06
      let contentRect = NSRect(x: margin, y: margin,
        width: size.width - margin * 2, height: size.height - margin * 2)
      let radius = contentRect.width * 0.2237
      let path = NSBezierPath(roundedRect: contentRect, xRadius: radius, yRadius: radius)
      path.addClip()
      icon.draw(in: contentRect)
      maskedIcon.unlockFocus()
      NSApp.applicationIconImage = maskedIcon
    }

    _ = NotificationService.shared
    _ = UpdaterViewModel.shared

    // Auto-configure local auth on every launch
    AuthService.shared.configure()

    // Subscribe to marrow Python reasoning_loop proactive notifications
    MarrowProactiveService.shared.start()

    TierManager.migrateExistingUsersIfNeeded()
    if !UserDefaults.standard.bool(forKey: "hasLaunchedBefore") {
      UserDefaults.standard.set(true, forKey: "hasLaunchedBefore")
      UserDefaults.standard.set(0, forKey: "currentTierLevel")
      UserDefaults.standard.set(0, forKey: "lastSeenTierLevel")
      UserDefaults.standard.set(true, forKey: "userShowAllFeatures")
    }

    let userId = UserDefaults.standard.string(forKey: "auth_userId")
    RewindDatabase.currentUserId = (userId?.isEmpty == false) ? userId : "anonymous"

    ResourceMonitor.shared.start()

    Task {
      await TranscriptionRetryService.shared.recoverPendingTranscriptions()
      TranscriptionRetryService.shared.start()
    }

    RecurringTaskScheduler.shared.start()

    if AuthState.shared.isSignedIn {
      AuthService.shared.fetchConversations()
      APIKeyService.shared.startFetchingKeys()
      Task { await FloatingBarUsageLimiter.shared.fetchPlan() }
      Task { await TierManager.shared.checkTierIfNeeded() }
    }

    migrateLaunchAtLoginDefault()

    updateOnboardingLifecyclePolicy(reason: "launch")
    userDefaultsObserver = NotificationCenter.default.addObserver(
      forName: UserDefaults.didChangeNotification, object: nil, queue: .main
    ) { [weak self] _ in
      self?.updateOnboardingLifecyclePolicy(reason: "user_defaults_changed")
    }

    NSAppleEventManager.shared().setEventHandler(
      self,
      andSelector: #selector(handleGetURLEvent(_:withReplyEvent:)),
      forEventClass: AEEventClass(kInternetEventClass),
      andEventID: AEEventID(kAEGetURL)
    )

    setupGlobalHotkeys()
    GlobalShortcutManager.shared.registerShortcuts()
    NSApp.setActivationPolicy(.regular)

    Task { @MainActor in self.setupMenuBar() }

    Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
      DispatchQueue.main.async {
        guard let self = self else { return }
        let item = self.statusBarItem
        let button = item?.button
        let isPhantom = button != nil && button!.frame.width == 0
        if item?.isVisible != true || button == nil || isPhantom {
          self.setupMenuBar()
        }
      }
    }

    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
      NSApp.activate()
      for window in NSApp.windows {
        if window.title.hasPrefix("Marrow") || window.title.hasPrefix("omi") {
          window.makeKeyAndOrderFront(nil)
          window.appearance = NSAppearance(named: .darkAqua)
          window.collectionBehavior.insert(.fullScreenPrimary)
        }
      }
    }

    log("AppDelegate: applicationDidFinishLaunching completed")
  }

  private func stripProvenanceXattrs() {
    let bundlePath = Bundle.main.bundlePath
    DispatchQueue.global(qos: .utility).async {
      let process = Process()
      process.launchPath = "/usr/bin/xattr"
      process.arguments = ["-cr", bundlePath]
      process.standardOutput = nil; process.standardError = nil
      try? process.run(); process.waitUntilExit()
    }
  }

  private func setupGlobalHotkeys() {
    let hotkeyHandler: (NSEvent) -> NSEvent? = { event in
      let modifiers = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
      let isCtrlOption = modifiers.contains(.control) && modifiers.contains(.option)
      let isR = event.keyCode == 15
      if isCtrlOption && isR {
        DispatchQueue.main.async {
          NSApp.activate()
          for window in NSApp.windows {
            if window.title.hasPrefix("Marrow") { window.makeKeyAndOrderFront(nil); break }
          }
          NotificationCenter.default.post(name: .navigateToRewind, object: nil)
        }
      }
      return event
    }
    globalHotkeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { _ = hotkeyHandler($0) }
    localHotkeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { hotkeyHandler($0) }
  }

  @MainActor private func setupMenuBar() {
    if let old = statusBarItem { NSStatusBar.system.removeStatusItem(old); statusBarItem = nil }
    statusBarItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    guard let statusBarItem = statusBarItem else { return }

    let displayName = "Marrow"
    if let button = statusBarItem.button {
      if MarrowApp.launchMode == .rewind {
        if let icon = NSImage(systemSymbolName: "clock.arrow.circlepath", accessibilityDescription: "Marrow Rewind") {
          icon.isTemplate = true; button.image = icon
        }
      } else if let iconURL = Bundle.resourceBundle.url(forResource: "omi_text_logo", withExtension: "png"),
        let icon = NSImage(contentsOf: iconURL)
      {
        icon.isTemplate = true
        let aspect = icon.size.width / icon.size.height
        icon.size = NSSize(width: 16 * aspect, height: 16)
        button.image = icon; button.imagePosition = .imageOnly
      } else {
        if let icon = NSImage(systemSymbolName: "waveform", accessibilityDescription: "Marrow") {
          icon.isTemplate = true; button.image = icon
        }
      }
      button.toolTip = MarrowApp.launchMode == .rewind ? "Marrow Rewind" : displayName
    }

    let menu = NSMenu()

    let screenCaptureItem = NSMenuItem()
    screenCaptureItem.view = makeToggleItemView(title: "Screen Capture", iconName: "rectangle.dashed.badge.record",
      isOn: AssistantSettings.shared.screenAnalysisEnabled && ProactiveAssistantsPlugin.shared.isMonitoring,
      action: #selector(screenCaptureToggled(_:)))
    menu.addItem(screenCaptureItem)

    let audioRecordingItem = NSMenuItem()
    audioRecordingItem.view = makeToggleItemView(title: "Audio Recording", iconName: "mic.fill",
      isOn: AssistantSettings.shared.transcriptionEnabled, action: #selector(audioRecordingToggled(_:)))
    menu.addItem(audioRecordingItem)

    menu.addItem(NSMenuItem.separator())

    let openItem = NSMenuItem(title: "Open \(displayName)", action: #selector(openOmiFromMenu), keyEquivalent: "o")
    openItem.target = self; menu.addItem(openItem)
    menu.addItem(NSMenuItem.separator())

    if AuthState.shared.isSignedIn, let email = AuthState.shared.userEmail {
      let emailItem = NSMenuItem(title: "User: \(email)", action: nil, keyEquivalent: "")
      emailItem.isEnabled = false; menu.addItem(emailItem)
      menu.addItem(NSMenuItem.separator())
    }

    let reportItem = NSMenuItem(title: "Report Issue...", action: #selector(reportIssue), keyEquivalent: "")
    reportItem.target = self; menu.addItem(reportItem)
    menu.addItem(NSMenuItem.separator())

    let quitItem = NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "q")
    quitItem.target = self; menu.addItem(quitItem)

    statusBarItem.menu = menu; menu.delegate = self
  }

  @MainActor @objc private func openOmiFromMenu() {
    NSApp.activate()
    var found = false
    for window in NSApp.windows {
      if window.frame.width > 300 && window.frame.height > 200 && !window.title.hasPrefix("Item-") {
        window.makeKeyAndOrderFront(nil); found = true; break
      }
    }
    if !found { Self.openMainWindow?() }
  }

  @MainActor @objc private func reportIssue() {
    FeedbackWindow.show(userEmail: AuthState.shared.userEmail)
  }

  @MainActor @objc private func quitApp() {
    NSApplication.shared.terminate(nil)
  }

  private func makeToggleItemView(title: String, iconName: String, isOn: Bool, action: Selector) -> NSView {
    let height: CGFloat = 36, width: CGFloat = 260
    let view = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
    let iconView = NSImageView(frame: NSRect(x: 16, y: 10, width: 16, height: 16))
    let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
    if let img = NSImage(systemSymbolName: iconName, accessibilityDescription: title)?.withSymbolConfiguration(config) {
      iconView.image = img; iconView.contentTintColor = .secondaryLabelColor
    }
    view.addSubview(iconView)
    let label = NSTextField(labelWithString: title)
    label.frame = NSRect(x: 40, y: 10, width: 150, height: 16)
    label.font = .systemFont(ofSize: 13); label.textColor = .labelColor
    view.addSubview(label)
    let toggle = NSSwitch()
    toggle.controlSize = .small; toggle.state = isOn ? .on : .off
    toggle.target = self; toggle.action = action; toggle.sizeToFit()
    let toggleX = width - toggle.frame.width - 16
    let toggleY = (height - toggle.frame.height) / 2
    toggle.frame = NSRect(x: toggleX, y: toggleY, width: toggle.frame.width, height: toggle.frame.height)
    toggle.autoresizingMask = [.minXMargin]
    view.addSubview(toggle)
    if action == #selector(screenCaptureToggled(_:)) { screenCaptureSwitch = toggle }
    else if action == #selector(audioRecordingToggled(_:)) { audioRecordingSwitch = toggle }
    return view
  }

  @MainActor @objc private func screenCaptureToggled(_ sender: NSSwitch) {
    let enabled = sender.state == .on
    if enabled {
      if !ProactiveAssistantsPlugin.shared.hasScreenRecordingPermission {
        sender.state = .off; ProactiveAssistantsPlugin.shared.openScreenRecordingPreferences(); return
      }
      AssistantSettings.shared.screenAnalysisEnabled = true
      ProactiveAssistantsPlugin.shared.startMonitoring { success, _ in
        DispatchQueue.main.async { if !success { sender.state = .off; AssistantSettings.shared.screenAnalysisEnabled = false } }
      }
    } else {
      AssistantSettings.shared.screenAnalysisEnabled = false
      ProactiveAssistantsPlugin.shared.stopMonitoring()
    }
  }

  @MainActor @objc private func audioRecordingToggled(_ sender: NSSwitch) {
    let enabled = sender.state == .on
    AssistantSettings.shared.transcriptionEnabled = enabled
    NotificationCenter.default.post(name: .toggleTranscriptionRequested, object: nil,
      userInfo: ["enabled": enabled])
  }

  func menuWillOpen(_ menu: NSMenu) {
    screenCaptureSwitch?.state = ProactiveAssistantsPlugin.shared.isMonitoring ? .on : .off
    audioRecordingSwitch?.state = AssistantSettings.shared.transcriptionEnabled ? .on : .off
  }

  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
    !UserDefaults.standard.bool(forKey: "hasCompletedOnboarding")
  }

  func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
    for window in sender.windows where window.title.hasPrefix("Marrow") {
      if window.isMiniaturized { window.deminiaturize(nil) }
      window.makeKeyAndOrderFront(nil); sender.activate(ignoringOtherApps: true); return false
    }
    return true
  }

  func applicationWillTerminate(_ notification: Notification) {
    UserDefaults.standard.set(true, forKey: "lastSessionCleanExit")
    for observer in windowObservers { NotificationCenter.default.removeObserver(observer) }
    windowObservers.removeAll()
    if let observer = userDefaultsObserver { NotificationCenter.default.removeObserver(observer) }
    if let monitor = globalHotkeyMonitor { NSEvent.removeMonitor(monitor) }
    if let monitor = localHotkeyMonitor { NSEvent.removeMonitor(monitor) }
    GlobalShortcutManager.shared.unregisterShortcuts()
    PushToTalkManager.shared.cleanup()
    TranscriptionRetryService.shared.stop()
    RecurringTaskScheduler.shared.stop()
    RewindDatabase.markCleanShutdown()
    ResourceMonitor.shared.stop()
  }

  @objc func handleGetURLEvent(_ event: NSAppleEventDescriptor, withReplyEvent replyEvent: NSAppleEventDescriptor) {
    guard let urlString = event.paramDescriptor(forKeyword: AEKeyword(keyDirectObject))?.stringValue,
      let url = URL(string: urlString) else { return }
    Task { @MainActor in
      AuthService.shared.handleOAuthCallback(url: url)
      NSApp.activate()
    }
  }

  private func migrateLaunchAtLoginDefault() {
    let migrationKey = "didMigrateLaunchAtLoginV1"
    guard !UserDefaults.standard.bool(forKey: migrationKey) else { return }
    UserDefaults.standard.set(true, forKey: migrationKey)
    let hasCompletedOnboarding = UserDefaults.standard.bool(forKey: "hasCompletedOnboarding")
    guard hasCompletedOnboarding else { return }
    Task { @MainActor in
      let manager = LaunchAtLoginManager.shared
      if !manager.isEnabled { _ = manager.setEnabled(true) }
    }
  }

  private func updateOnboardingLifecyclePolicy(reason: String) {
    let hasCompletedOnboarding = UserDefaults.standard.bool(forKey: "hasCompletedOnboarding")
    if hasCompletedOnboarding {
      guard relaunchOnLoginSuppressedForOnboarding else { return }
      NSApp.enableRelaunchOnLogin(); relaunchOnLoginSuppressedForOnboarding = false; return
    }
    guard !relaunchOnLoginSuppressedForOnboarding else { return }
    NSApp.disableRelaunchOnLogin(); relaunchOnLoginSuppressedForOnboarding = true
  }

  func applicationDidBecomeActive(_ notification: Notification) {
    Task { await SettingsSyncManager.shared.syncFromServer() }
  }
}
