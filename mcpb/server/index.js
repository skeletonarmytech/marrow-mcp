#!/usr/bin/env node
/*
 * marrow-bridge: stdio <-> Streamable HTTP shim for Claude Desktop.
 *
 * Claude Desktop's one-click extensions (.mcpb) launch a local stdio server,
 * but Marrow's MCP endpoint is an HTTP URL on the user's phone or their
 * self-hosted mirror. This bridge forwards newline-delimited JSON-RPC from
 * stdin to that URL with the bearer token, and writes responses to stdout.
 *
 * Zero dependencies on purpose: Claude Desktop ships its own Node runtime
 * and nothing should need installing.
 */
const URL_ = process.env.MARROW_URL;
const TOKEN = process.env.MARROW_TOKEN;

if (!URL_ || !TOKEN) {
  process.stderr.write("marrow-bridge: MARROW_URL and MARROW_TOKEN are required\n");
  process.exit(1);
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (line) forward(line);
  }
});

async function forward(line) {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const isNotification = msg.id === undefined;
  try {
    const res = await fetch(URL_, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": `Bearer ${TOKEN}`,
      },
      body: line,
    });
    // Notifications get no reply on stdio; drain and move on.
    if (isNotification) { await res.text(); return; }
    const text = await res.text();
    if (res.ok && text.trim()) {
      process.stdout.write(text.trim() + "\n");
    } else {
      reply(msg.id, -32000, `Marrow answered ${res.status}. Check the URL and token in the extension settings, and that the server is on.`);
    }
  } catch (err) {
    if (!isNotification)
      reply(msg.id, -32001, `Could not reach ${URL_}: ${err.message}. Is the phone or mirror on the same network?`);
  }
}

function reply(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");
}
