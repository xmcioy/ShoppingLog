// 本地 OCR：macOS Vision framework 文字识别（不联网、不调模型 API）
// 用法: ocr_vision <image_path>
// 输出: 每行识别文本（按自上而下顺序）
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: ocr_vision <image_path>\n".data(using: .utf8)!)
    exit(1)
}
let path = args[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot load image: \(path)\n".data(using: .utf8)!)
    exit(2)
}
let req = VNRecognizeTextRequest { request, error in
    if error != nil {
        FileHandle.standardError.write("ocr error: \(error!.localizedDescription)\n".data(using: .utf8)!)
        exit(3)
    }
    let obs = (request.results as? [VNRecognizedTextObservation]) ?? []
    // 按 y 坐标（视觉坐标，y 大在上）排序，保证阅读顺序
    let sorted = obs.sorted { a, b in
        if abs(a.boundingBox.midY - b.boundingBox.midY) > 0.02 {
            return a.boundingBox.midY > b.boundingBox.midY
        }
        return a.boundingBox.minX < b.boundingBox.minX
    }
    var out = ""
    for o in sorted {
        if let t = o.topCandidates(1).first {
            out += t.string + "\n"
        }
    }
    FileHandle.standardOutput.write(out.data(using: .utf8)!)
}
req.recognitionLevel = .accurate
req.recognitionLanguages = ["zh-Hans", "en-US"]
req.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
do {
    try handler.perform([req])
} catch {
    FileHandle.standardError.write("perform error: \(error)\n".data(using: .utf8)!)
    exit(4)
}
