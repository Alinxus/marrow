import Foundation

/// Routes chat queries to the local Marrow Python server (http://localhost:8888).
/// Same query() interface as ACPBridge — ChatProvider picks this when bridgeMode == .marrowLocal.
actor MarrowLocalBridge {
    private let baseURL = "http://localhost:8888"

    func query(
        prompt: String,
        systemPrompt: String,
        sessionKey: String? = nil,
        cwd: String? = nil,
        mode: String? = nil,
        model: String? = nil,
        resume: String? = nil,
        imageData: Data? = nil,
        onTextDelta: @escaping ACPBridge.TextDeltaHandler,
        onToolCall: @escaping ACPBridge.ToolCallHandler,
        onToolActivity: @escaping ACPBridge.ToolActivityHandler,
        onThinkingDelta: @escaping ACPBridge.ThinkingDeltaHandler = { _ in },
        onToolResultDisplay: @escaping ACPBridge.ToolResultDisplayHandler = { _, _, _ in },
        onAuthRequired: @escaping ACPBridge.AuthRequiredHandler = { _, _ in },
        onAuthSuccess: @escaping ACPBridge.AuthSuccessHandler = {}
    ) async throws -> ACPBridge.QueryResult {
        var body: [String: Any] = [
            "text": prompt,
            "system_prompt": systemPrompt,
        ]
        if let model { body["model"] = model }
        if let sessionKey { body["session_key"] = sessionKey }

        guard let url = URL(string: "\(baseURL)/v1/chat/messages") else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.timeoutInterval = 120

        let (data, response) = try await URLSession.shared.data(for: request)

        if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
                ?? "HTTP \(http.statusCode)"
            throw NSError(
                domain: "MarrowLocalBridge", code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: detail])
        }

        guard
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let text = json["text"] as? String
        else {
            throw NSError(
                domain: "MarrowLocalBridge", code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid response from local server"])
        }

        let sid = sessionKey ?? "main"
        let inputTokens = json["input_tokens"] as? Int ?? 0
        let outputTokens = json["output_tokens"] as? Int ?? 0
        let costUsd = json["cost_usd"] as? Double ?? 0.0

        // Simulate token-streaming: deliver ~80 words/sec so the chat UI shows progressive output.
        let words = text.components(separatedBy: " ")
        for (i, word) in words.enumerated() {
            let chunk = i == 0 ? word : " \(word)"
            onTextDelta(chunk)
            try await Task.sleep(nanoseconds: 12_000_000)
        }

        return ACPBridge.QueryResult(
            text: text,
            costUsd: costUsd,
            sessionId: sid,
            inputTokens: inputTokens,
            outputTokens: outputTokens,
            cacheReadTokens: 0,
            cacheWriteTokens: 0
        )
    }
}
