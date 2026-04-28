import Foundation

/// Calls marrow's Python server to check and open OS permission panels.
/// Used by PermissionsPage to surface Python-side permission status
/// (e.g. AppleScript accessibility, sounddevice microphone, mss screen capture).
@MainActor
class MarrowPermissionsClient {
    static let shared = MarrowPermissionsClient()
    private init() {}

    /// Fetch a human-readable permission report from the Python side.
    func checkPermissions() async -> String {
        guard let url = URL(string: "\(APIClient.shared.baseURL)v1/permissions/check") else {
            return "Permission check unavailable (bad URL)."
        }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let report = obj["report"] as? String {
                return report
            }
            return "Permission check returned unexpected response."
        } catch {
            return "Permission check failed: \(error.localizedDescription)"
        }
    }

    /// Ask the Python server to open OS permission settings panels.
    func openPermissionPanels() async -> String {
        guard let url = URL(string: "\(APIClient.shared.baseURL)v1/permissions/open") else {
            return "Cannot open permission panels (bad URL)."
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data("{}".utf8)
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let result = obj["result"] as? String {
                return result
            }
            return "Permission panel request returned unexpected response."
        } catch {
            return "Failed to open permission panels: \(error.localizedDescription)"
        }
    }
}
