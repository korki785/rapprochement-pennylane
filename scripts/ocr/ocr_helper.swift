import Foundation
import Vision
import AppKit

// Usage: ocr <image_path>  -> prints recognized text (fr+en) to stdout
guard CommandLine.arguments.count >= 2 else {
    FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!)
    exit(2)
}
let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot load image\n".data(using: .utf8)!)
    exit(3)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.usesLanguageCorrection = true
req.recognitionLanguages = ["fr-FR", "en-US"]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try handler.perform([req])
var out = ""
for obs in (req.results ?? []) {
    if let top = obs.topCandidates(1).first { out += top.string + "\n" }
}
print(out)
