"""Claude support shares Pi's TTY navigation, filtering and read-only snapshots."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_agent_switcher import a, agent, pane, process


def claude_process(**kwargs):
    return process(command="claude", args="claude", **kwargs)


def claude_agent(**kwargs):
    item = agent(agent="claude", **kwargs)
    item.update(command="claude", args="claude")
    return item


class ClaudeDiscoveryTests(unittest.TestCase):
    def test_native_title_and_npm_processes(self):
        for command, args in (
            ("claude", "claude"),
            ("/home/me/.local/bin/claude", "claude --resume"),
            ("/home/me/.local/share/claude/versions/2.1.0", "claude"),
            ("node", "node /opt/@anthropic-ai/claude-code/cli.js"),
            ("bun", "bun /opt/@anthropic-ai/claude-code/cli.js"),
        ):
            with self.subTest(command=command, args=args):
                self.assertEqual(a.agent_kind(process(command=command, args=args)), "claude")
        for command, args in (("bash", "bash -c 'claude'"), ("node", "node claude.js"),
                              ("claude-helper", "claude-helper"), ("bash", "'")):
            self.assertEqual(a.agent_kind(process(command=command, args=args)), "")

    def test_mixed_agents_use_tty_and_ignore_stale_pi_names(self):
        processes = {101: process(), 102: claude_process(pid=102, tty="ttys002")}
        panes = [pane(), pane("%2", "/dev/ttys002", name="old Pi task")]
        result = a.find_agents(processes, panes, "$1")
        self.assertCountEqual([(x["pane_id"], x["agent"]) for x in result], [("%1", "pi"), ("%2", "claude")])
        self.assertEqual(a.display_name(next(x for x in result if x["agent"] == "claude")), "repo")

    def test_headless_and_background_processes_are_excluded(self):
        for args in ("claude -p prompt", "claude --print prompt", "claude --print=prompt",
                     "claude daemon run", "claude bg-pty-host", "claude bg-spare",
                     "node /opt/@anthropic-ai/claude-code/cli.js daemon run"):
            with self.subTest(args=args):
                p = process(command="node" if args.startswith("node ") else "claude", args=args)
                self.assertEqual(a.find_agents({101: p}, [pane()], "$1"), [])
        self.assertEqual(a.find_agents({101: claude_process(tty="??")}, [pane()], "$1"), [])
        # Prompt text is not a background command.
        p = process(command="claude", args='claude "explain bg-spare"')
        self.assertEqual(len(a.find_agents({101: p}, [pane()], "$1")), 1)

    def test_cross_kind_and_same_kind_nested_workers_are_excluded(self):
        for parent in (process(), claude_process()):
            for child in (process(pid=102, parent=101), claude_process(pid=102, parent=101)):
                result = a.find_agents({101: parent, 102: child}, [pane()], "$1")
                self.assertEqual([x["pid"] for x in result], [101])

    def test_foreground_claude_wins_over_suspended_pi_on_same_tty(self):
        result = a.find_agents({101: process(state="T"), 102: claude_process(pid=102)}, [pane()], "$1")
        self.assertEqual([(x["pid"], x["agent"]) for x in result], [(102, "claude")])

    def test_liveness_checks_agent_kind_and_tty(self):
        for text, expected in (("101 100 ttys001 S+ claude claude", True),
                               ("101 100 ttys002 S+ claude claude", False),
                               ("101 100 ttys001 S+ pi pi", False), ("", False)):
            with patch.object(a, "run", return_value=subprocess.CompletedProcess([], 0, text, "")):
                self.assertEqual(a.still_running(claude_agent()), expected)

    def test_discovery_scans_only_stores_for_live_agent_kinds(self):
        pi, claude = agent(agent="pi"), claude_agent()
        for items, kinds in (([pi], ["pi"]), ([claude], ["claude"]), ([pi, claude], ["pi", "claude"]), ([], [])):
            with patch.object(a, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                 patch.object(a, "find_agents", return_value=items), patch.object(a, "process_details"), \
                 patch.object(a, "session_candidates", return_value=[]) as candidates:
                self.assertEqual(a.discover("$1"), items)
                self.assertEqual([call.args for call in candidates.call_args_list], [({"/repo"}, k) for k in kinds])


class ClaudeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def session(self, name="session", cwd="/repo", entries=()):
        path = self.root / "projects" / "project" / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [dict(type="file-history-snapshot", snapshot={}),
                   dict(type="user", cwd=cwd, sessionId=name, timestamp="2026-09-01T12:00:00Z",
                        message=dict(role="user", content="first prompt")), *entries]
        path.write_text("\n".join(json.dumps(e) for e in records) + "\n")
        return path

    def test_claude_layout_header_scan_and_config_override(self):
        expected = self.session()
        self.session("unrelated", cwd="/other")
        self.session("relative", cwd="relative")
        self.session("subagents/child")
        self.session("agent-old-sidechain")
        sidechain = self.session("sidechain")
        sidechain.write_text(json.dumps(dict(cwd="/repo", isSidechain=True)) + "\n")
        path = self.session("no-header")
        path.write_text("[]\nnull\ninvalid\n" * 40 + json.dumps(dict(cwd="/repo")) + "\n")
        alias = self.root / "projects/alias"
        alias.symlink_to(expected.parent, target_is_directory=True)
        candidates = a.session_candidates({"/repo"}, "claude")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["path"], str(expected.resolve()))
        self.assertEqual(candidates[0]["agent"], "claude")
        self.assertEqual(candidates[0]["session_id"], "session")
        self.assertGreater(candidates[0]["created"], 0)

    def test_malformed_prefix_is_ignored(self):
        path = self.session()
        path.write_bytes(b'\xff\n[]\n{"unfinished"\n' + path.read_bytes())
        self.assertEqual(len(a.session_candidates({"/repo"}, "claude")), 1)

    def test_metadata_titles_model_prompt_and_message_time(self):
        path = self.session(entries=[
            dict(type="custom-title", customTitle="Old title"),
            dict(type="custom-title", customTitle="Fix\tlogin\n\x1b"),
            dict(type="ai-title", aiTitle="Automatic title"),
            dict(type="user", message=dict(content=[dict(type="text", text="latest prompt")])),
            dict(type="assistant", timestamp="2026-09-21T12:00:00Z", effort="high",
                 message=dict(model="claude-test-model", content=[dict(type="thinking", thinking="private")])),
            dict(type="user", timestamp="2026-09-21T12:01:00Z", toolUseResult="output",
                 message=dict(content=[dict(type="tool_result", content="tool output"),
                                       dict(type="text", text="also tool output")])),
            dict(type="user", isMeta=True, message=dict(content="internal instruction")),
            dict(type="assistant", isSidechain=True, message=dict(model="other-model")),
            dict(type="last-prompt", lastPrompt="not a timestamped message"),
        ])
        with path.open("a") as f:
            f.write('[]\nnull\n{"type":"unfinished"')
        data = a.session_metadata(path, "claude")
        self.assertEqual(data["name"], "Fix login")
        self.assertEqual(data["model"], "claude-test-model")
        self.assertEqual(data["thinking"], "high")
        self.assertEqual(data["prompt"], "latest prompt")
        self.assertEqual(data["last_message"], a.timestamp("2026-09-21T12:01:00Z"))
        self.assertEqual(data["last_role"], "user")
        self.assertNotIn("private", str(data))

    def test_latest_title_wins_and_empty_custom_title_uses_auto_title(self):
        path = self.session(entries=[dict(type="custom-title", customTitle="old"),
                                    dict(type="custom-title", customTitle=""),
                                    dict(type="ai-title", aiTitle="old auto"),
                                    dict(type="ai-title", aiTitle="new auto")])
        self.assertEqual(a.session_metadata(path, "claude")["name"], "new auto")

    def test_metadata_ignores_old_payloads_once_messages_are_known(self):
        path = self.session(entries=[
            dict(type="custom-title", customTitle="Task"),
            *[dict(type="user", message=dict(content="x" * 500)) for _ in range(200)],
            dict(type="user", message=dict(content="last prompt")),
            dict(type="assistant", timestamp="2026-09-21T12:00:00Z", message=dict(model="test-model")),
        ])
        with patch.object(a.json, "loads", wraps=a.json.loads) as loads:
            data = a.session_metadata(path, "claude")
        self.assertEqual(data["name"], "Task")
        self.assertEqual(data["prompt"], "last prompt")
        self.assertLess(loads.call_count, 10)

    def test_matching_is_one_to_one_and_never_crosses_agent_kinds(self):
        first = self.session("first", entries=[dict(type="ai-title", aiTitle="Claude task")])
        second = self.session("second")
        os.utime(first, (300, 300))
        os.utime(second, (200, 200))
        pi, claude, other = agent(), claude_agent(), claude_agent()
        claude["@pi_session_name"] = "Stale Pi name"
        other["started"] = 50
        candidates = a.session_candidates({"/repo"}, "claude")
        a.match_sessions([pi, other, claude], candidates)
        self.assertNotIn("saved", pi)
        self.assertEqual(claude["saved"]["path"], str(first.resolve()))
        self.assertEqual(other["saved"]["path"], str(second.resolve()))
        self.assertEqual(a.display_name(claude), "Claude task")
        self.assertEqual(claude["match"], "cwd + recency")
        fresh = claude_agent()
        a.match_sessions([fresh], [dict(path="/missing/pi.jsonl", cwd="/repo", mtime=999, agent="pi")])
        self.assertNotIn("saved", fresh)

    def test_ephemeral_claude_does_not_borrow_saved_metadata(self):
        self.session()
        item = claude_agent()
        item["args"] = "claude --no-session-persistence"
        a.match_sessions([item], a.session_candidates({"/repo"}, "claude"))
        self.assertNotIn("saved", item)

    def test_preview_refreshes_claude_metadata_not_pi_metadata(self):
        path = self.session(entries=[dict(type="ai-title", aiTitle="Updated Claude title")])
        item = claude_agent(saved=dict(path=str(path), name="Old title"), match="cwd + recency")
        with patch.object(a, "load_agent", return_value=item), patch.object(a, "still_running", return_value=True), \
             patch.object(a, "run", return_value=subprocess.CompletedProcess([], 0, "screen\n", "")), \
             patch("builtins.print") as output:
            a.preview("ignored", "%1")
        printed = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("Updated Claude title", printed)
        self.assertIn("Claude", printed)
        self.assertIn("first prompt", printed)
        self.assertIn("best-effort", printed)
        self.assertNotIn("Old title", printed)

    @unittest.skipUnless(shutil.which("fzf"), "fzf unavailable")
    def test_claude_title_filter_keeps_heading_and_labels_kind(self):
        pi = agent()
        claude = claude_agent(saved=dict(name="Fix login", last_message=200))
        claude.update(pane_id="%2", window_index="1")
        claude["@pi_session_name"] = "Stale task"
        data = a.rows([pi, claude], "$1", "%1", "'login")
        fields = [line.split("\t") for line in data.splitlines()]
        self.assertEqual([row[1] for row in fields], ["$1", "%2"])
        self.assertEqual(fields[0][0], "%2")
        self.assertIn("[Claude]", fields[1][2])
        self.assertNotIn("[Pi]", data)
        self.assertNotIn("Stale", data)
        # The type badge is decoration, not part of the name query.
        self.assertEqual(a.rows([pi, claude], "$1", "%1", "'Claude"), "")


if __name__ == "__main__":
    unittest.main()
