"""Router regression guard (ADR 0100).

The safety property under test: an ambiguous message — a trigger-score tie, or no trigger hit at
all — must fall to the read-only ``general`` specialist, never to a write-capable pack. This was
once broken (`max()` returned the first maximal key, which is ``manager``, the *most* privileged
pack) and survived because no router test existed. These tests are the guard.
"""

from skipper import skills
from skipper.router import choose, choose_from_messages, latest_user_text, score


class TestSingleSpecialistRouting:
    """A clear single-specialist message routes to that specialist."""

    def test_manager(self):
        assert choose("please retrain jpcp on the latest data") == "manager"

    def test_monitor(self):
        assert choose("is there any drift right now?") == "monitor"

    def test_helper(self):
        assert choose("how do i configure the pipeline") == "helper"

    def test_finops(self):
        assert choose("what did we spend this month") == "finops"

    def test_governor(self):
        assert choose("show me the audit trail") == "governor"


class TestTieFallsToGeneral:
    """The regression guard: tied scores resolve to the read-only generalist."""

    def test_manager_monitor_tie(self):
        # 'drift' (monitor) + 'promote' (manager) — one hit each.
        text = "check the drift and then promote jpcp"
        s = score(text)
        assert s["manager"] == s["monitor"] > 0
        assert choose(text) == skills.GENERAL.name

    def test_manager_governor_tie(self):
        # 'audit' (governor) + 'retrain' (manager).
        text = "show the audit log and retrain jpcp"
        s = score(text)
        assert s["manager"] == s["governor"] > 0
        assert choose(text) == skills.GENERAL.name

    def test_monitor_finops_tie(self):
        # 'spend' (finops) + 'drift' (monitor).
        text = "compare spend and drift"
        s = score(text)
        assert s["finops"] == s["monitor"] > 0
        assert choose(text) == skills.GENERAL.name

    def test_no_tie_ever_reaches_a_write_pack(self):
        # Property sweep: for every cross-specialist trigger pair that ties, the router must
        # return 'general'. (Pairs where one trigger contains the other, or where a trigger also
        # matches help-intent markers, legitimately don't tie and are skipped.)
        specialists = {s.name: s for s in skills.SPECIALISTS}
        for a in specialists.values():
            for b in specialists.values():
                if a.name >= b.name:
                    continue
                text = f"{a.triggers[0]} and {b.triggers[0]}"
                s = score(text)
                if s[a.name] == s[b.name] and s[a.name] > 0:
                    got = choose(text)
                    if got != "helper":  # help-intent precedence is allowed to win
                        assert got == skills.GENERAL.name, (
                            f"tie between {a.name}/{b.name} ({text!r}) routed to {got}"
                        )


class TestZeroHitFallsToGeneral:
    def test_no_trigger(self):
        assert choose("hello there") == skills.GENERAL.name

    def test_empty(self):
        assert choose("") == skills.GENERAL.name

    def test_none_like(self):
        assert choose_from_messages([]) == skills.GENERAL.name


class TestHelpIntentPrecedence:
    """Interrogative/capability phrasing routes to the docs helper over the named action."""

    def test_how_do_i_deploy(self):
        assert choose("how do i deploy a model") == "helper"

    def test_capability_question(self):
        assert choose("does examlops support canary rollouts?") == "helper"

    def test_can_i_promote(self):
        assert choose("can i promote without an approval?") == "helper"

    def test_operational_verb_keeps_specialist(self):
        # "can I see/check …" is an operational request, not a learning one.
        assert choose("can i check the drift status?") == "monitor"


class TestLatestUserText:
    def test_picks_last_human(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        assert latest_user_text(msgs) == "second"

    def test_multimodal_join(self):
        msgs = [{"role": "user", "content": [{"text": "a"}, {"text": "b"}]}]
        assert latest_user_text(msgs) == "a b"
