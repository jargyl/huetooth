"""Schedule rules: when they are due, catching up, once per occurrence.

Run:  py -m unittest tests.test_schedule -v
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hue_config as config  # noqa: E402
import hue_schedule as schedule  # noqa: E402

RED = [0.675, 0.322]


def rule(**kw):
    base = {"id": "S1", "enabled": True, "time": "22:00", "until": "04:00", "days": list(range(7)),
            "lights": "all", "action": {"type": "color", "xy": RED, "brightness": 200}, "last_run": None}
    base.update(kw)
    return base


# 2026-10-14 is a Wednesday (weekday 2).
WED = datetime(2026, 10, 14)


def at(day: datetime, hhmm: str, plus_days: int = 0) -> datetime:
    h, m = (int(v) for v in hhmm.split(":"))
    return day.replace(hour=h, minute=m) + timedelta(days=plus_days)


class ParseTests(unittest.TestCase):
    def test_times(self):
        for text, expected in [("22", "22:00"), ("22:00", "22:00"), ("2200", "22:00"), ("22.30", "22:30"),
                               ("22h", "22:00"), ("22h30", "22:30"), ("22u30", "22:30"), ("7:05", "07:05"),
                               ("730", "07:30"), (" 0:00 ", "00:00")]:
            self.assertEqual(schedule.parse_time(text), expected, text)
        for text in ["", "24:00", "22:60", "abc", "7:5", "22:000"]:
            self.assertIsNone(schedule.parse_time(text), text)


class DueTests(unittest.TestCase):
    def test_fires_at_its_time_and_only_once(self):
        r = rule()
        self.assertIsNone(schedule.due(r, at(WED, "21:59")))
        moment = schedule.due(r, at(WED, "22:00"))
        self.assertEqual(moment, at(WED, "22:00"))
        r["last_run"] = moment.strftime(schedule.STAMP)
        self.assertIsNone(schedule.due(r, at(WED, "22:05")))          # done: changing the lights later sticks
        self.assertEqual(schedule.due(r, at(WED, "22:00", 1)), at(WED, "22:00", 1))   # tomorrow again

    def test_catches_up_until_the_until_time_across_midnight(self):
        r = rule()
        self.assertEqual(schedule.due(r, at(WED, "23:30")), at(WED, "22:00"))
        self.assertEqual(schedule.due(r, at(WED, "03:59", 1)), at(WED, "22:00"))     # Thursday night, still on
        self.assertIsNone(schedule.due(r, at(WED, "04:01", 1)))                     # too late now
        self.assertIsNone(schedule.due(r, at(WED, "12:00", 1)))

    def test_without_until_only_right_at_the_time(self):
        r = rule(until=None)
        self.assertEqual(schedule.due(r, at(WED, "22:01")), at(WED, "22:00"))       # small margin
        self.assertIsNone(schedule.due(r, at(WED, "22:10")))

    def test_days(self):
        r = rule(days=[4], until="23:00")                            # Fridays only
        self.assertIsNone(schedule.due(r, at(WED, "22:30")))
        friday = WED + timedelta(days=2)
        self.assertEqual(schedule.due(r, at(friday, "22:30")), at(friday, "22:00"))
        late = rule(days=[4], until="02:00")                         # a Friday night going into Saturday
        self.assertEqual(schedule.due(late, at(friday, "01:00", 1)), at(friday, "22:00"))

    def test_switched_off(self):
        self.assertIsNone(schedule.due(rule(enabled=False), at(WED, "22:00")))

    def test_next_occurrence_and_wait(self):
        r = rule(days=[0])                                          # Mondays
        self.assertEqual(schedule.next_occurrence(r, at(WED, "12:00")), datetime(2026, 10, 19, 22, 0))
        wait = schedule.seconds_until_next([rule(), rule(id="S2", time="21:00")], at(WED, "20:00"))
        self.assertEqual(wait, 3600)
        self.assertIsNone(schedule.seconds_until_next([rule(enabled=False)], at(WED, "20:00")))


class ActionTests(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(schedule.action_fields({"type": "off"}), {"power": False})
        self.assertEqual(schedule.action_fields({"type": "color", "xy": RED, "brightness": 120}),
                         {"power": True, "xy": tuple(RED), "brightness": 120, "effect": 0})
        self.assertEqual(schedule.action_fields({"type": "white", "mireds": 370, "brightness": 90}),
                         {"power": True, "mireds": 370, "brightness": 90, "effect": 0})
        self.assertIsNone(schedule.action_fields({"type": "scene", "scene": "Sunset"}))

    def test_days_text(self):
        self.assertEqual(schedule.describe_days(list(range(7))), "Every day")
        self.assertEqual(schedule.describe_days([0, 1, 2, 3, 4]), "Weekdays")
        self.assertEqual(schedule.describe_days([5, 6]), "Weekends")
        self.assertEqual(schedule.describe_days([0, 2]), "Mon, Wed")


class CleanTests(unittest.TestCase):
    def test_settings_file_values_are_checked(self):
        raw = [rule(),
               {"time": "7.30", "action": {"type": "white", "mireds": 9999, "brightness": 999}, "days": [1, 9, "x"]},
               {"time": "25:00", "action": {"type": "off"}},                 # bad time
               {"time": "08:00", "action": {"type": "disco"}},               # unknown action
               {"time": "08:00", "action": {"type": "scene"}},               # scene without a name
               "nonsense"]
        rules = schedule.clean(raw)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[1]["time"], "07:30")
        self.assertEqual(rules[1]["action"], {"type": "white", "mireds": 500, "brightness": 254})
        self.assertEqual(rules[1]["days"], [1])
        self.assertEqual(rules[1]["lights"], "all")
        self.assertNotEqual(rules[0]["id"], rules[1]["id"])

    def test_loaded_with_the_settings(self):
        import json
        import tempfile

        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"schedules": [rule()], "hidden_scenes": ["Movie", "Not a scene"]}))
        cfg = config.load_config(path)
        self.assertEqual(cfg["schedules"][0]["time"], "22:00")
        self.assertEqual(cfg["hidden_scenes"], ["Movie"])
        self.assertNotIn("Movie", [s["name"] for s in config.all_scenes(cfg)])
        self.assertEqual(config.find_scene(cfg, "movie")["name"], "Movie")      # still usable by name


if __name__ == "__main__":
    unittest.main()
