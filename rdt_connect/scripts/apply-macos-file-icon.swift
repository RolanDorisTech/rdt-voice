import AppKit
import Foundation

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: apply-macos-file-icon.swift ICON_PNG TARGET_FILE\n", stderr)
    exit(2)
}

let iconPath = CommandLine.arguments[1]
let targetPath = CommandLine.arguments[2]

guard let image = NSImage(contentsOfFile: iconPath) else {
    fputs("Could not load icon PNG: \(iconPath)\n", stderr)
    exit(3)
}

guard NSWorkspace.shared.setIcon(image, forFile: targetPath, options: []) else {
    fputs("Could not set Finder custom icon: \(targetPath)\n", stderr)
    exit(4)
}

print("PASS: Finder custom icon applied: \(targetPath)")
