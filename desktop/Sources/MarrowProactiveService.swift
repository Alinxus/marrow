import Foundation
import SwiftUI

/// Subscribes to marrow's Python reasoning loop via WebSocket and delivers
/// proactive notifications to the FloatingControlBar.
///
/// Flow:
///   Python reasoning_loop fires → push_proactive_event() → /v1/proactive/stream WS
///   → MarrowProactiveService receives → posts FloatingBarNotification to state
@MainActor
class MarrowProactiveService: ObservableObject {
    static let shared = MarrowProactiveService()

    private var webSocketTask: URLSessionWebSocketTask?
    private var urlSession: URLSession?
    private var isConnected = false
    private var reconnectTask: Task<Void, Never>?
    private var shouldReconnect = true

    /// Set this to deliver notifications to the floating bar
    var onNotification: ((FloatingBarNotification) -> Void)?

    private init() {}

    func start() {
        shouldReconnect = true
        connect()
    }

    func stop() {
        shouldReconnect = false
        reconnectTask?.cancel()
        reconnectTask = nil
        webSocketTask?.cancel(with: .goingAway, reason: nil)
        webSocketTask = nil
        isConnected = false
    }

    private func connect() {
        let baseURL = APIClient.shared.baseURL
        // Build ws:// URL from http://
        let wsBase = baseURL
            .replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")
        guard let url = URL(string: "\(wsBase)v1/proactive/stream") else { return }

        let session = URLSession(configuration: .default)
        self.urlSession = session
        let task = session.webSocketTask(with: url)
        self.webSocketTask = task
        task.resume()
        isConnected = true
        log("MarrowProactiveService: Connected to \(url)")
        receiveLoop(task)
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor [weak self] in
                guard let self else { return }
                switch result {
                case .success(let msg):
                    self.handleMessage(msg)
                    self.receiveLoop(task)
                case .failure(let error):
                    log("MarrowProactiveService: Receive error: \(error.localizedDescription)")
                    self.isConnected = false
                    self.scheduleReconnect()
                }
            }
        }
    }

    private func handleMessage(_ msg: URLSessionWebSocketTask.Message) {
        var jsonData: Data?
        switch msg {
        case .string(let str): jsonData = str.data(using: .utf8)
        case .data(let d): jsonData = d
        @unknown default: return
        }
        guard let data = jsonData,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return }

        let type_ = obj["type"] as? String ?? ""
        guard type_ == "notification" else { return }

        let title = obj["title"] as? String ?? "Marrow"
        let message = obj["message"] as? String ?? ""
        guard !message.isEmpty else { return }

        let ctx = obj["context"] as? [String: Any]
        let urgency = ctx?["urgency"] as? Double ?? 3
        let assistantId = obj["assistant_id"] as? String ?? "marrow"

        let notification = FloatingBarNotification(
            title: title,
            message: message,
            assistantId: assistantId,
            context: FloatingBarNotificationContext(
                sourceTitle: title,
                assistantId: assistantId,
                sourceApp: nil,
                windowTitle: nil,
                contextSummary: message,
                currentActivity: nil,
                reasoning: nil,
                detail: nil
            )
        )

        log("MarrowProactiveService: Notification urgency=\(urgency) — \(message.prefix(80))")
        onNotification?(notification)

        // Deliver directly to FloatingControlBar if no custom handler
        if onNotification == nil {
            FloatingControlBarManager.shared.showNotification(
                title: title,
                message: message,
                assistantId: assistantId,
                sound: .default,
                context: notification.context
            )
        }
    }

    private func scheduleReconnect() {
        guard shouldReconnect else { return }
        reconnectTask?.cancel()
        reconnectTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000) // 5s
            guard let self, self.shouldReconnect else { return }
            self.connect()
        }
    }
}
