import SwiftUI
import UIKit

/// A UITextView with syntax colours from the analyzer's spans. SwiftUI's
/// TextEditor only takes an AttributedString from iOS 26, so this wraps
/// UIKit; selection, keyboard, dictation and undo stay native.
struct RelevanceTextView: UIViewRepresentable {
    @Binding var text: String
    var spans: [Span]

    func makeUIView(context: Context) -> UITextView {
        let view = UITextView()
        view.font = .monospacedSystemFont(ofSize: 16, weight: .regular)
        view.autocapitalizationType = .none
        view.autocorrectionType = .no
        view.smartQuotesType = .no  // relevance needs straight quotes
        view.smartDashesType = .no
        view.backgroundColor = .secondarySystemBackground
        view.layer.cornerRadius = 8
        view.delegate = context.coordinator
        view.text = text
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        if view.text != text { view.text = text }
        apply(spans, to: view)
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: RelevanceTextView
        init(_ parent: RelevanceTextView) { self.parent = parent }
        func textViewDidChange(_ view: UITextView) { parent.text = view.text }
    }

    private func apply(_ spans: [Span], to view: UITextView) {
        let text = view.text ?? ""
        // Python offsets count code points; NSString counts UTF-16 units.
        var utf16: [Int] = [0]
        for scalar in text.unicodeScalars { utf16.append(utf16.last! + scalar.utf16.count) }
        let storage = view.textStorage
        let all = NSRange(location: 0, length: storage.length)
        guard storage.length == utf16.last! else { return }
        storage.beginEditing()
        storage.removeAttribute(.underlineStyle, range: all)
        storage.addAttribute(.foregroundColor, value: UIColor.label, range: all)
        for span in spans where span.end <= utf16.count - 1 && span.start < span.end {
            let range = NSRange(location: utf16[span.start], length: utf16[span.end] - utf16[span.start])
            switch span.kind {
            case "keyword": storage.addAttribute(.foregroundColor, value: UIColor.systemBlue, range: range)
            case "string": storage.addAttribute(.foregroundColor, value: UIColor.systemRed, range: range)
            case "number": storage.addAttribute(.foregroundColor, value: UIColor.systemPurple, range: range)
            case "comment": storage.addAttribute(.foregroundColor, value: UIColor.systemGray, range: range)
            case "punct": storage.addAttribute(.foregroundColor, value: UIColor.secondaryLabel, range: range)
            case "error", "warning":
                storage.addAttribute(.underlineStyle, value: NSUnderlineStyle.thick.rawValue | NSUnderlineStyle.patternDot.rawValue, range: range)
                storage.addAttribute(.underlineColor, value: span.kind == "error" ? UIColor.systemRed : UIColor.systemOrange, range: range)
            default: break
            }
        }
        storage.endEditing()
    }
}
