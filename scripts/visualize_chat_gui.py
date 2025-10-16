#!/usr/bin/env python3
"""
Web-based GUI for visualizing JSONL evaluation logs.
Uses only Python standard library - no external dependencies.
"""

import http.server
import json
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chat Conversation Viewer</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: "Avenir", -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 800px;
            margin: auto;
            background: #f5f5f5;
            color: #333;
            line-height: 1.6;
        }
        .header {
            background: #667eea;
            color: white;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .header h1 { font-size: 24px; margin-bottom: 5px; }
        .header .subtitle { opacity: 0.9; font-size: 14px; }
        .stats {
            background: white;
            padding: 15px 20px;
            border-bottom: 1px solid #e0e0e0;
            display: flex;
            gap: 30px;
            flex-wrap: wrap;
        }
        .stat { display: flex; align-items: center; gap: 8px; }
        .stat-label { color: #666; font-size: 13px; }
        .stat-value { font-weight: 600; color: #333; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .message {
            background: white;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .message-header {
            padding: 12px 16px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: 1px solid #e0e0e0;
        }
        .message-header.user { background: #f0f7ff; border-left: 4px solid #2196F3; }
        .message-header.assistant { background: #f1f8f4; border-left: 4px solid #4CAF50; }
        .message-header.system { background: #fff8f0; border-left: 4px solid #FF9800; }
        .message-header.result { background: #f3f0ff; border-left: 4px solid #9C27B0; }
        .message-icon { font-size: 20px; }
        .message-meta {
            flex: 1;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .message-type { font-weight: 600; font-size: 14px; }
        .message-time {
            color: #666;
            font-size: 12px;
            font-family: 'Menlo', monospace;
        }
        .message-content { padding: 16px; }
        .text-content {
            white-space: pre-wrap;
            word-wrap: break-word;
            color: #333;
            line-height: 1.6;
        }
        .tool-call {
            background: #fff9e6;
            border: 1px solid #ffd54f;
            border-radius: 6px;
            padding: 12px;
            margin: 8px 0;
        }
        .tool-call-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
            font-weight: 600;
            color: #f57c00;
        }
        .tool-call-name {
            font-family: 'Menlo', monospace;
            background: #fff;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 13px;
        }
        .tool-call-params {
            background: white;
            border-radius: 4px;
            padding: 8px;
            margin-top: 8px;
            font-family: 'Menlo', monospace;
            font-size: 12px;
            max-height: 300px;
            overflow-y: auto;
        }
        .param-row { padding: 4px 0; border-bottom: 1px solid #f0f0f0; }
        .param-row:last-child { border-bottom: none; }
        .param-key { color: #1976d2; font-weight: 600; }
        .param-value { color: #333; margin-left: 10px; }
        .tool-result {
            background: #e8f5e9;
            border: 1px solid #81c784;
            border-radius: 6px;
            padding: 12px;
            margin: 8px 0;
        }
        .tool-result.error { background: #ffebee; border-color: #e57373; }
        .tool-result-header {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
            font-weight: 600;
        }
        .tool-result-header.success { color: #388e3c; }
        .tool-result-header.error { color: #d32f2f; }
        .tool-result-content {
            background: white;
            border-radius: 4px;
            padding: 8px;
            font-family: 'Menlo', monospace;
            font-size: 12px;
            max-height: 400px;
            overflow-y: auto;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .expand-toggle {
            cursor: pointer;
            color: #1976d2;
            text-decoration: underline;
            font-size: 12px;
            margin-top: 4px;
            display: inline-block;
        }
        .expand-toggle:hover { color: #1565c0; }
        .collapsed {
            max-height: 100px;
            overflow: hidden;
            position: relative;
        }
        .collapsed::after {
            content: '';
            position: absolute;
            bottom: 0; left: 0; right: 0;
            height: 30px;
            background: linear-gradient(transparent, white);
        }
        .filter-bar {
            background: white;
            padding: 15px 20px;
            border-bottom: 1px solid #e0e0e0;
            display: flex;
            gap: 15px;
            align-items: center;
            flex-wrap: wrap;
        }
        .filter-bar label {
            display: flex;
            align-items: center;
            gap: 5px;
            cursor: pointer;
            font-size: 14px;
        }
        .filter-bar input[type="checkbox"] { cursor: pointer; }
        .result-summary {
            background: white;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }
        .result-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 10px;
        }
        .result-item { padding: 10px; background: #f5f5f5; border-radius: 4px; }
        .result-item-label { font-size: 12px; color: #666; margin-bottom: 4px; }
        .result-item-value { font-size: 18px; font-weight: 600; color: #333; }
        .success-badge {
            display: inline-block;
            background: #4CAF50;
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .error-badge {
            display: inline-block;
            background: #f44336;
            color: white;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .hidden { display: none; }
        code {
            background: #f5f5f5;
            padding: 2px 6px;
            border-radius: 3px;
            font-family: 'Menlo', monospace;
            font-size: 13px;
        }
        .search-box { flex: 1; min-width: 200px; }
        .search-box input {
            width: 100%;
            padding: 8px 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        .search-box input:focus { outline: none; border-color: #667eea; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🔍 Chat Conversation Viewer</h1>
        <div class="subtitle">Interactive evaluation log inspector</div>
    </div>
    <div class="stats" id="stats">
        <div class="stat">
            <span class="stat-label">Total Messages:</span>
            <span class="stat-value" id="total-messages">0</span>
        </div>
        <div class="stat">
            <span class="stat-label">Tool Calls:</span>
            <span class="stat-value" id="total-tools">0</span>
        </div>
        <div class="stat">
            <span class="stat-label">Errors:</span>
            <span class="stat-value" id="total-errors">0</span>
        </div>
    </div>
    <div class="filter-bar">
        <div class="search-box">
            <input type="text" id="search" placeholder="Search messages...">
        </div>
        <label><input type="checkbox" id="filter-user" checked> 👤 User</label>
        <label><input type="checkbox" id="filter-assistant" checked> 🤖 Assistant</label>
        <label><input type="checkbox" id="filter-system" checked> ⚙️ System</label>
        <label><input type="checkbox" id="filter-tools" checked> 🔧 Tool Calls</label>
        <label><input type="checkbox" id="filter-errors"> ❌ Errors Only</label>
    </div>
    <div class="container" id="messages-container"></div>
    <script src="/viewer.js"></script>
    <script>
        fetch("/data.json", { cache: "no-store" })
            .then(response => {
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                return response.json();
            })
            .then(data => initViewer(data))
            .catch(error => {
                console.error("Failed to load conversation data:", error);
                const container = document.getElementById("messages-container");
                if (container) {
                    container.innerHTML = "<p style='color:#d32f2f'>Unable to load conversation data. Check the server logs for details.</p>";
                }
            });
    </script>
</body>
</html>
"""


class LogViewerHandler(http.server.SimpleHTTPRequestHandler):
    jsonl_data = []
    script_path = ""
    font_path = ""

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())
        elif self.path == "/viewer.js":
            self.send_response(200)
            self.send_header("Content-type", "application/javascript")
            self.end_headers()
            with open(self.script_path, "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/data.json":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.jsonl_data).encode("utf-8"))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def start_server(jsonl_file, port=8000):
    script_dir = Path(__file__).parent
    js_file = script_dir / "viewer.js"

    if not js_file.exists():
        print(f"Error: viewer.js not found at {js_file}")
        sys.exit(1)

    print(f"Loading {jsonl_file}...")
    with open(jsonl_file, "r") as f:
        data = []
        for line in f:
            try:
                data.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue

    print(f"Loaded {len(data)} entries")

    LogViewerHandler.jsonl_data = data
    LogViewerHandler.script_path = js_file

    while port < 9000:
        try:
            with socketserver.TCPServer(("", port), LogViewerHandler) as httpd:
                url = f"http://localhost:{port}"
                print(f"\n{'=' * 60}")
                print(f"🚀 Server running at: {url}")
                print(f"{'=' * 60}")
                print(f"📁 Serving viewer.js from: {js_file}")
                print("Press Ctrl+C to stop the server")
                threading.Timer(1.0, lambda: webbrowser.open(url)).start()
                httpd.serve_forever()
        except OSError:
            port += 1
            continue
        break


def main():
    if len(sys.argv) < 2:
        print("Usage: python visualize_chat_gui.py <path_to_jsonl_file> [port]")
        print("\nExample:")
        print("  python visualize_chat_gui.py results/example.jsonl")
        print("  python visualize_chat_gui.py results/example.jsonl 8080")
        sys.exit(1)

    jsonl_file = Path(sys.argv[1])

    if not jsonl_file.exists():
        print(f"Error: File not found: {jsonl_file}")
        sys.exit(1)

    port = 8000
    if len(sys.argv) > 2:
        try:
            port = int(sys.argv[2])
        except ValueError:
            print(f"Warning: Invalid port '{sys.argv[2]}', using default 8000")

    try:
        start_server(jsonl_file, port)
    except KeyboardInterrupt:
        print("\n\n✅ Server stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
