import Foundation

private let logFile: String = {
  let isDev = AppBuild.isNonProduction
  return isDev ? "/tmp/marrow-dev.log" : "/tmp/marrow.log"
}()
private let logQueue = DispatchQueue(label: "ai.marrow.logger", qos: .utility)
private let dateFormatter: DateFormatter = {
  let formatter = DateFormatter()
  formatter.dateFormat = "HH:mm:ss.SSS"
  return formatter
}()

private func appendToLogFile(_ line: String) {
  guard let data = (line + "\n").data(using: .utf8) else { return }
  logQueue.async { writeToLogFile(data) }
}

private func appendToLogFileSync(_ line: String) {
  guard let data = (line + "\n").data(using: .utf8) else { return }
  logQueue.sync { writeToLogFile(data) }
}

private func writeToLogFile(_ data: Data) {
  if FileManager.default.fileExists(atPath: logFile) {
    if let handle = FileHandle(forWritingAtPath: logFile) {
      handle.seekToEndOfFile()
      handle.write(data)
      handle.closeFile()
    }
  } else {
    FileManager.default.createFile(atPath: logFile, contents: data)
  }
}

func logPerf(_ message: String, duration: Double? = nil, cpu: Bool = false) {
  let timestamp = dateFormatter.string(from: Date())
  var parts = ["[\(timestamp)] [perf] \(message)"]
  if let duration = duration {
    parts.append(String(format: "(%.1fms)", duration * 1000))
  }
  if cpu {
    var usage = rusage()
    if getrusage(RUSAGE_SELF, &usage) == 0 {
      let u = Double(usage.ru_utime.tv_sec) + Double(usage.ru_utime.tv_usec) / 1_000_000
      let s = Double(usage.ru_stime.tv_sec) + Double(usage.ru_stime.tv_usec) / 1_000_000
      parts.append(String(format: "[cpu: user=%.2fs sys=%.2fs]", u, s))
    }
  }
  let line = parts.joined(separator: " ")
  print(line); fflush(stdout)
  appendToLogFile(line)
}

class PerfTimer {
  private let name: String
  private let start: CFAbsoluteTime
  private let logCPU: Bool
  init(_ name: String, logCPU: Bool = false) {
    self.name = name; self.start = CFAbsoluteTimeGetCurrent(); self.logCPU = logCPU
  }
  func stop() { logPerf(name, duration: CFAbsoluteTimeGetCurrent() - start, cpu: logCPU) }
  func checkpoint(_ label: String) { logPerf("\(name) → \(label)", duration: CFAbsoluteTimeGetCurrent() - start) }
}

func measurePerf<T>(_ name: String, logCPU: Bool = false, _ block: () -> T) -> T {
  let timer = PerfTimer(name, logCPU: logCPU); let result = block(); timer.stop(); return result
}

func measurePerfAsync<T>(_ name: String, logCPU: Bool = false, _ block: () async -> T) async -> T {
  let timer = PerfTimer(name, logCPU: logCPU); let result = await block(); timer.stop(); return result
}

func logSync(_ message: String) {
  let line = "[\(dateFormatter.string(from: Date()))] [app] \(message)"
  print(line); fflush(stdout)
  appendToLogFileSync(line)
}

func log(_ message: String) {
  let line = "[\(dateFormatter.string(from: Date()))] [app] \(message)"
  print(line); fflush(stdout)
  appendToLogFile(line)
}

func logError(_ message: String, error: Error? = nil) {
  let errorDesc = error?.localizedDescription ?? ""
  let fullMessage = error != nil ? "\(message): \(errorDesc)" : message
  let line = "[\(dateFormatter.string(from: Date()))] [error] \(fullMessage)"
  print(line); fflush(stdout)
  appendToLogFile(line)
}
