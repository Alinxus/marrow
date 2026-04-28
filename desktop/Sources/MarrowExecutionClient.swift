import Foundation

/// Calls marrow's Python execution_engine via /v1/execute.
///
/// The Python side runs the task through Claude with a rich tool set:
/// shell commands, file I/O, browser control, clipboard, window management,
/// AppleScript (macOS), and more — all sandboxed behind the approval layer.
///
/// Swift only needs to describe what to do in plain text; Python handles
/// permission checks, platform branching, and tool selection.
@MainActor
class MarrowExecutionClient {
    static let shared = MarrowExecutionClient()
    private init() {}

    struct ExecuteResult {
        let ok: Bool
        let output: String
    }

    /// Execute a plain-English task description.
    /// Returns the execution output or an error message.
    func execute(task: String, context: String = "") async -> ExecuteResult {
        guard let url = URL(string: "\(APIClient.shared.baseURL)v1/execute") else {
            return ExecuteResult(ok: false, output: "Execution unavailable (bad URL).")
        }

        let body: [String: Any] = ["action": task, "context": context]
        guard let bodyData = try? JSONSerialization.data(withJSONObject: body) else {
            return ExecuteResult(ok: false, output: "Failed to encode request.")
        }

        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = bodyData
        // Execution can take a while — raise timeout to 120s
        req.timeoutInterval = 120

        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return ExecuteResult(ok: false, output: "Unexpected response from execution engine.")
            }
            let status = obj["status"] as? String ?? "error"
            let result = obj["result"] as? String ?? ""
            return ExecuteResult(ok: status == "ok", output: result)
        } catch {
            return ExecuteResult(ok: false, output: "Execution request failed: \(error.localizedDescription)")
        }
    }
}
