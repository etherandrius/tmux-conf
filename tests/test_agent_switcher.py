"""Run with: python3 -m unittest discover -s tests -v"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "agents", Path(__file__).resolve().parents[1] / "tmux-agent-switcher.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def pane(pid="%1", tty="/dev/ttys001", session="$1", name="", cwd="/repo"):
    values = [pid, tty, "100", session, "work", "@1", "0", "repo", "0", cwd,
              name, "blue", "100"]
    return dict(zip(a.PANE_FIELDS, values))


def process(pid=101, parent=100, tty="ttys001", command="pi", args="pi", state="S+"):
    return dict(pid=pid, parent=parent, tty=tty, command=command, args=args, state=state)


def agent(**kwargs):
    return dict(pane(), **process(), cwd="/repo", started=100, **kwargs)


class DiscoveryTests(unittest.TestCase):
    def test_parse_ps_and_pi_identification(self):
        output = "101 100 ttys001 S+ pi pi\n102 100 ?? S /usr/bin/node node unrelated.js\ninvalid\n"
        processes = a.parse_processes(output)
        self.assertTrue(a.is_pi(processes[101]))
        self.assertFalse(a.is_pi(processes[102]))
        self.assertTrue(a.is_pi(process(command="node", args="node /opt/@earendil-works/pi-coding-agent/dist/cli.js")))
        self.assertFalse(a.is_pi(process(command="bash", args="bash -c 'echo pi'")))

    def test_navigation_uses_tty_not_cwd_or_current_command(self):
        panes = [pane(), pane("%2", "/dev/ttys002")]
        agents = a.find_agents({101: process(tty="ttys002")}, panes, "$1")
        self.assertEqual([item["pane_id"] for item in agents], ["%2"])

    def test_excludes_headless_nested_and_non_tmux_processes(self):
        processes = {
            101: process(),
            102: process(102, parent=101, tty="??"),
            103: process(103, parent=101),  # Inherited controlling terminal.
            104: process(104, tty="ttys002", args="pi --mode rpc"),
            105: process(105, tty="ttys003", args="pi -p prompt"),
            106: process(106, tty="ttys999"),
        }
        panes = [pane(), pane("%2", "/dev/ttys002"), pane("%3", "/dev/ttys003")]
        self.assertEqual([item["pid"] for item in a.find_agents(processes, panes, "$1")], [101])

    def test_ancestry_cycle_does_not_hang(self):
        processes = {101: process(parent=102), 102: process(102, parent=102, command="bash", args="bash")}
        self.assertFalse(a.ancestor_is_pi(processes[101], processes))

    def test_linked_window_prefers_current_session(self):
        panes = [pane(session="$1"), pane(session="$2")]
        result = a.find_agents({101: process()}, panes, "$2")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["session_id"], "$2")

    def test_multiple_jobs_on_same_tty_prefer_foreground_once(self):
        result = a.find_agents({101: process(), 102: process(102, state="T")}, [pane()], "$1")
        self.assertEqual([item["pid"] for item in result], [101])

    def test_empty_tmux_fields_keep_their_positions(self):
        p = pane()
        parsed = a.parse_panes("\t".join(p.values()) + "\n")
        self.assertEqual(parsed, [p])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def session(self, name="session.jsonl", cwd="/repo", entries=()):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = dict(type="session", id=name, cwd=cwd, timestamp="2026-09-01T12:00:00Z")
        path.write_text("\n".join(json.dumps(entry) for entry in [header, *entries]) + "\n")
        return path

    def candidate(self, path, mtime=200, created=100):
        return dict(path=str(path), cwd="/repo", mtime=mtime, created=created)

    def test_metadata_latest_wins_and_partial_write_is_ignored(self):
        path = self.session(entries=[
            dict(type="session_info", name="old"),
            dict(type="model_change", provider="old", modelId="model"),
            dict(type="thinking_level_change", thinkingLevel="low"),
            dict(type="message", message=dict(role="user", content="fix\nthis")),
            dict(type="message", timestamp="2026-09-21T12:00:00Z",
                 message=dict(role="assistant", provider="openai", model="latest")),
            dict(type="model_change", provider="anthropic", modelId="new selection"),
            dict(type="thinking_level_change", thinkingLevel="high"),
            dict(type="session_info", name="new"),
        ])
        with path.open("a") as handle:
            handle.write('{"type":"unfinished"')
        data = a.session_metadata(path)
        self.assertEqual(data["name"], "new")
        self.assertEqual(data["model"], "new selection")
        self.assertEqual(data["thinking"], "high")
        self.assertEqual(data["prompt"], "fix this")
        self.assertEqual(data["last_role"], "assistant")
        self.assertGreater(data["last_message"], 0)

    def test_empty_name_does_not_restore_old_name(self):
        path = self.session(entries=[dict(type="session_info", name="old"), dict(type="session_info", name="")])
        self.assertEqual(a.session_metadata(path)["name"], "")

    def test_large_record_across_reverse_reader_blocks(self):
        path = self.session(entries=[dict(type="session_info", name="big"),
                                    dict(type="custom", data="x" * 200000),
                                    dict(type="message", message=dict(role="user", content="last", timestamp=123000))])
        data = a.session_metadata(path)
        self.assertEqual(data["name"], "big")
        self.assertEqual(data["last_message"], 123)

    def test_missing_session_and_non_object_records(self):
        self.assertEqual(a.session_metadata(self.root / "missing"), {})
        path = self.session()
        with path.open("a") as handle:
            handle.write('[]\nnull\n42\n')
        self.assertEqual(a.session_metadata(path), {})

    def test_literal_metadata_cannot_inject_rows_or_escapes(self):
        self.assertEqual(a.clean("name\tfoo\nbar\x1b\x00"), "name foo bar")
        self.assertEqual(a.text_content([dict(type="image", data="secret"), dict(type="text", text="hello")]), "hello")

    def test_flat_nested_and_symlink_stores_are_deduplicated(self):
        self.session()
        self.session("nested/other.jsonl")
        self.session("unrelated.jsonl", cwd="/other")
        (self.root / "invalid.jsonl").write_text("[]\n")
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with patch.object(a, "session_roots", return_value=[self.root, link]):
            self.assertEqual(len(a.session_candidates({"/repo"})), 2)

    def test_override_store_is_authoritative(self):
        with patch.dict(os.environ, {"PI_CODING_AGENT_SESSION_DIR": str(self.root)}):
            self.assertEqual(a.session_roots(), [self.root])

    def test_named_agents_claim_own_file_before_unnamed_agent(self):
        one = self.session("one.jsonl", entries=[dict(type="session_info", name="Named task")])
        two = self.session("two.jsonl")
        unnamed, named = agent(), agent()
        named["@pi_session_name"] = "Named task"
        a.match_sessions([unnamed, named], [self.candidate(one, mtime=300), self.candidate(two)])
        self.assertEqual(named["saved"]["path"], str(one))
        self.assertEqual(unnamed["saved"]["path"], str(two))

    def test_new_session_in_old_process_prefers_recent_activity(self):
        old, new = self.session("old.jsonl"), self.session("new.jsonl")
        item = agent()
        a.match_sessions([item], [self.candidate(old), self.candidate(new, mtime=400, created=350)])
        self.assertEqual(item["saved"]["path"], str(new))
        self.assertEqual(item["match"], "cwd + recency")

    def test_named_matching_reads_full_metadata_only_for_selected_file(self):
        old = self.session("old.jsonl", entries=[dict(type="session_info", name="old task")])
        current = self.session("current.jsonl", entries=[dict(type="session_info", name="live task")])
        item = agent()
        item["@pi_session_name"] = "live task"
        with patch.object(a, "session_metadata", wraps=a.session_metadata) as metadata, \
             patch.object(a, "session_name", wraps=a.session_name) as names:
            a.match_sessions([item], [self.candidate(old), self.candidate(current, mtime=300)])
        self.assertEqual(metadata.call_args_list, [unittest.mock.call(current)])
        self.assertEqual(names.call_args_list, [unittest.mock.call(current)])

    def test_metadata_skips_decoding_old_messages_once_fields_are_known(self):
        path = self.session(entries=[
            dict(type="thinking_level_change", thinkingLevel="high"),
            *[dict(type="message", message=dict(role="toolResult", content="x" * 100)) for _ in range(200)],
            dict(type="message", message=dict(role="user", content="latest")),
            dict(type="message", message=dict(role="assistant", provider="p", model="m")),
        ])
        with patch.object(a.json, "loads", wraps=a.json.loads) as loads:
            data = a.session_metadata(path)
        self.assertEqual(data["thinking"], "high")
        self.assertEqual(data["prompt"], "latest")
        self.assertLess(loads.call_count, 10)

    def test_explicit_ephemeral_agent_does_not_borrow_saved_metadata(self):
        path = self.session()
        item = agent()
        item["args"] = "pi --no-session"
        a.match_sessions([item], [self.candidate(path)])
        self.assertNotIn("saved", item)

    def test_unmatched_name_does_not_borrow_someone_elses_metadata(self):
        path = self.session(entries=[dict(type="session_info", name="other")])
        item = agent()
        item["@pi_session_name"] = "live"
        a.match_sessions([item], [self.candidate(path)])
        self.assertNotIn("saved", item)


class UITests(unittest.TestCase):
    def test_current_pane_and_current_session_first(self):
        current, same_session, other = agent(), agent(), agent()
        current["pane_id"] = "%2"
        current["saved"] = dict(last_message=1)
        other["pane_id"], other["session_id"] = "%3", "$3"
        other["saved"] = dict(last_message=99999)
        with patch.object(a, "age", return_value="2m ago"):
            result = a.rows([other, same_session, current], "$1", "%2")
        lines = [line.split("\t") for line in result.splitlines()]
        self.assertEqual([line[1] for line in lines], ["$1", "%2", "%1", "$3", "%3"])
        self.assertEqual(lines[0][2], "* ▾ \033[2m(2m ago)\033[0m \033[34mwork\033[0m")
        self.assertEqual(lines[1][2], "    ├─ * \033[2m~2m ago\033[0m repo \033[2mrepo\033[0m")
        self.assertEqual(lines[2][2], "    └─   \033[2mtime unknown\033[0m repo \033[2mrepo\033[0m")
        self.assertEqual(lines[3][2], "  ▾ \033[2m(2m ago)\033[0m \033[34mwork\033[0m")
        self.assertEqual(lines[4][2], "    └─   \033[2m~2m ago\033[0m repo \033[2mrepo\033[0m")
        # Both the heading and its first child target the same real Pi pane.
        self.assertEqual(lines[0][0], lines[1][0])
        self.assertEqual(lines[3][0], lines[4][0])

    def test_sessions_stay_grouped_and_use_attachment_order(self):
        old, recent, sibling = agent(), agent(), agent()
        old["saved"] = dict(last_message=100)
        recent.update(pane_id="%2", session_id="$2", session_name="recent",
                      session_last_attached="200", saved=dict(last_message=50))
        sibling.update(pane_id="%3", saved=dict(last_message=300))
        result = a.rows([old, recent, sibling], "$absent", "%absent")
        lines = [line.split("\t", 2) for line in result.splitlines()]
        self.assertEqual([line[1] for line in lines], ["$2", "%2", "$1", "%3", "%1"])
        self.assertIn("\033[34mrecent\033[0m", lines[0][2])
        self.assertEqual(sum("▾" in line[2] for line in lines), 2)

    def test_list_uses_spaces_and_places_time_before_names(self):
        item = agent(saved=dict(model="model-name", prompt="investigate auth", last_message=100))
        item["@pi_session_name"] = "Fix login"
        with patch.object(a.time, "time", return_value=220):
            data = a.rows([item], "$1", "%1")
        fields = [line.split("\t") for line in data.splitlines()]
        self.assertTrue(all(len(row) == 3 for row in fields))  # No visible tab stops.
        self.assertEqual([row[2] for row in fields], [
            "* ▾ \033[2m(2m ago)\033[0m \033[34mwork\033[0m",
            "    └─ * \033[2m~2m ago\033[0m Fix login \033[2mrepo\033[0m",
        ])
        self.assertNotIn("model-name", data)
        self.assertNotIn("investigate auth", data)

    def test_long_names_do_not_push_time_to_end(self):
        item = agent(saved=dict(last_message=100))
        item["@pi_session_name"] = "Long agent name " * 20
        item["session_name"] = "Long session name " * 20
        with patch.object(a.time, "time", return_value=220):
            labels = [line.split("\t")[2] for line in a.rows([item], "$1", "%1").splitlines()]
        self.assertTrue(labels[0].startswith("* ▾ \033[2m(2m ago)\033[0m "))
        self.assertTrue(labels[1].startswith("    └─ * \033[2m~2m ago\033[0m "))

    @unittest.skipUnless(shutil.which("fzf"), "fzf unavailable")
    def test_fzf_matches_names_but_not_preview_metadata_or_hidden_ids(self):
        item = agent(saved=dict(model="model-name", prompt="investigate auth", last_message=100))
        item.update(pane_id="%9876", cwd="/private/project-path")
        item["@pi_session_name"] = "Fix login"
        for query, expected in (("'login", True), ("'work", True), ("'auth", False),
                                ("'model-name", False), ("'project-path", False),
                                ("'ago", False), ("'9876", False)):
            with self.subTest(query=query):
                result = a.rows([item], "$1", "%9876", query)
                self.assertEqual(bool(result), expected)

    def test_empty_scan_writes_valid_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(a, "discover", return_value=[]):
            path = str(Path(directory) / "agents.json")
            self.assertEqual(a.refresh(path, "$1", "%1"), "")
            self.assertIsNone(a.load_agent(path, "%1"))
            cache = json.loads(Path(path).read_text())
            self.assertEqual(cache["agents"], {})
            self.assertGreaterEqual(cache["load_seconds"], 0)
            self.assertTrue(a.picker_output(path, "").startswith("\t\tLoaded in "))

    def test_stale_or_reused_pid_is_not_navigated_to(self):
        for output in ("", "101 100 ttys002 S+ pi pi", "101 100 ttys001 S+ bash bash"):
            with self.subTest(output=output), patch.object(a, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                self.assertFalse(a.still_running(agent()))

    def test_preview_contains_metadata_and_bottom_of_actual_screen(self):
        item = agent()
        with patch.object(a, "load_agent", return_value=item), patch.object(a, "still_running", return_value=True), \
             patch.dict(os.environ, {"FZF_PREVIEW_LINES": "10"}), patch("builtins.print") as output:
            def run(args, **kwargs):
                text = "main\n" if args[0] == "git" else "top\nmiddle\neditor\nfooter\n"
                return subprocess.CompletedProcess(args, 0, text, "")
            with patch.object(a, "run", side_effect=run):
                a.preview("ignored", "%1")
            printed = "\n".join(call.args[0] for call in output.call_args_list)
            self.assertIn("PID 101", printed)
            self.assertIn("Saved metadata unavailable", printed)
            self.assertIn("[main]", printed)
            self.assertIn("editor\nfooter", printed)
            self.assertNotIn("middle", printed)


@unittest.skipUnless(shutil.which("fzf"), "fzf unavailable")
class FilteringTests(unittest.TestCase):
    def setUp(self):
        cleanup, login, docs, other = agent(), agent(), agent(), agent()
        cleanup.update(saved=dict(last_message=600))
        cleanup["@pi_session_name"] = "Database cleanup"
        login.update(pane_id="%2", window_index="1", saved=dict(last_message=500))
        login["@pi_session_name"] = "Fix login"
        docs.update(pane_id="%3", session_id="$2", session_name="backend",
                    session_last_attached="200", saved=dict(last_message=10))
        docs["@pi_session_name"] = "Login docs"
        other.update(pane_id="%4", session_id="$3", session_name="frontend",
                     session_last_attached="300", saved=dict(last_message=900))
        other["@pi_session_name"] = "Unrelated"
        self.agents = [docs, other, login, cleanup]

    def filtered(self, query):
        return [line.split("\t") for line in a.rows(self.agents, "$1", "%1", query).splitlines()]

    def test_agent_match_keeps_nonmatching_heading_and_retargets_it(self):
        rows = self.filtered("'login")
        self.assertEqual([row[1] for row in rows], ["$1", "%2", "$2", "%3"])
        self.assertIn("work", rows[0][2])
        self.assertIn("backend", rows[2][2])
        self.assertEqual(rows[0][0], "%2")  # Not the filtered-out current Pi.
        self.assertIn("└─", rows[1][2])
        self.assertIn("└─", rows[3][2])

    def test_session_name_match_keeps_all_its_agents(self):
        rows = self.filtered("'work")
        self.assertEqual([row[1] for row in rows], ["$1", "%1", "%2"])
        self.assertIn("├─", rows[1][2])
        self.assertIn("└─", rows[2][2])

    def test_fzf_or_and_negation_syntax(self):
        rows = self.filtered("'work | 'docs")
        self.assertEqual([row[1] for row in rows], ["$1", "%1", "%2", "$2", "%3"])
        rows = self.filtered("'login !docs")
        self.assertEqual([row[1] for row in rows], ["$1", "%2"])

    def test_filtering_preserves_group_order_not_match_score(self):
        rows = self.filtered("'docs | 'Unrelated | 'login")
        self.assertEqual([row[1] for row in rows], ["$1", "%2", "$3", "%4", "$2", "%3"])

    def test_query_uses_cache_and_clearing_it_restores_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "agents.json"
            snapshot.write_text(json.dumps({"agents": {item["pane_id"]: item for item in self.agents},
                                            "load_seconds": 0.123}))
            original = snapshot.read_text()
            with patch.object(a, "discover", side_effect=AssertionError("Must not rescan")):
                filtered = a.filter_rows(snapshot, "$1", "%1", "'login")
                self.assertEqual(len(filtered.splitlines()), 4)
                self.assertEqual(a.filter_rows(snapshot, "$1", "%1", "no-such-name"), "")
                with patch.object(a, "matching_names", side_effect=AssertionError("Empty query needs no matcher")):
                    self.assertEqual(a.filter_rows(snapshot, "$1", "%1", ""),
                                     a.rows(self.agents, "$1", "%1"))
            self.assertEqual(snapshot.read_text(), original)

    def test_rescan_applies_query_but_caches_all_agents(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(a, "discover", return_value=self.agents):
            snapshot = Path(directory) / "agents.json"
            result = a.refresh(snapshot, "$1", "%1", "'login")
            self.assertEqual(len(result.splitlines()), 4)
            self.assertEqual(len(a.load_snapshot(snapshot)), 4)

    def test_batch_matcher_ignores_interactive_fzf_defaults(self):
        with patch.dict(os.environ, {"FZF_DEFAULT_OPTS": "--disabled --query=bogus",
                                     "FZF_DEFAULT_OPTS_FILE": "/missing/fzf-options"}):
            self.assertEqual([row[1] for row in self.filtered("'login")], ["$1", "%2", "$2", "%3"])


if __name__ == "__main__":
    unittest.main()
