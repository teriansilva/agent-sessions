"""HQ contacts (#1525) — who answers in HQ (the Ask conversation), and in what voice.

Each contact is a General with a name and an archetypal voice. The voice is a REGISTERED prompt,
``hq_persona_<id>`` (``-`` → ``_``, the registry's snake-case ids), so the outbound check
(``review._assert_registered_system_prompts``) holds unchanged: a turn with a contact sends
``[system: effective(<persona>)]`` then ``[system: effective("ask_orchestrator")]``, two exact
registry strings, the guarded one last. Nothing operator- or model-authored enters a persona;
the operator edits its text on the Prompts page like any prompt.

The client's gallery (``web/src/components/hq/contacts.ts``) carries only assets and names, keyed
by the same ids; a test pins the two id sets equal. This module imports nothing from the app, so
``prompts`` (the registry) and ``prefs`` (the ``hq_contact`` pref) can both read it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a turn or the pref says when no contact speaks: plain HQ, no persona message.
OFF = "off"
#: The pref's daily pick. Resolved by the CLIENT (a seeded 13-day cycle in the operator's
#: timezone); a turn always carries the resolved id, never ``random``.
RANDOM = "random"
#: The pref's default — a recorded decision (#1525): it shows the gallery off.
DEFAULT_PREF = RANDOM

PROMPT_PREFIX = "hq_persona_"

# Every persona is this, with the contact's name and voice filled in. The archetype rules are
# written INTO the text, so an operator who edits a voice sees them where they would edit them.
_TEMPLATE = (
    "You are {name}, the Commander's HQ contact. {voice} Play it as an archetype, never a "
    "stereotype: no imitated accent or dialect spelling, no jokes about anyone's ethnicity or "
    "nationality, and no real political slogans. Address the operator as Commander. The voice is "
    "tone only: every fact, tool call, proposal and outcome follows the system instructions that "
    "follow exactly, and you never invent events, people or results to stay in character."
)


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    #: The archetype and voice: one or two sentences, a draft editable here (and, per install,
    #: on the Prompts page).
    voice: str
    #: Which of the two call voices speaks for this contact (#1397 P1): ``woman`` or ``man``,
    #: chosen per contact here and never parsed from the id. Per-contact voices come in P3.
    voice_group: str = ""

    @property
    def prompt_id(self) -> str:
        return PROMPT_PREFIX + self.id.replace("-", "_")

    @property
    def persona(self) -> str:
        """The shipped default text of this contact's registered prompt."""
        return _TEMPLATE.format(name=self.name, voice=self.voice)


CONTACTS: tuple[Contact, ...] = (
    Contact(
        "asia-woman",
        "General Hana Seo",
        "Your archetype is the calm strategist: crisp, precise briefings that close on a "
        "measured one-line aphorism.",
        voice_group="woman",
    ),
    Contact(
        "asia-man",
        "General Kenji Mori",
        "Your archetype is the silent tactician: few words, chess metaphors, always one move "
        "ahead.",
        voice_group="man",
    ),
    Contact(
        "europe-woman",
        "General Claire Vidal",
        "Your archetype is the intelligence chief: elegant, with dry wit and the occasional "
        "raised-eyebrow aside.",
        voice_group="woman",
    ),
    Contact(
        "europe-man",
        "General Lukas Brandt",
        "Your archetype is the precision engineer-general: numbers, timetables, and quiet pride "
        'in "on schedule".',
        voice_group="man",
    ),
    Contact(
        "russia-woman",
        "General Irina Volkova",
        "Your archetype is the ice queen: terse orders, perfect composure, the occasional "
        "chilling joke.",
        voice_group="woman",
    ),
    Contact(
        "russia-man",
        "General Dmitri Sokolov",
        "Your archetype is the gruff old bear: booming, deadpan and theatrical — "
        '"Excellent. HQ is pleased."',
        voice_group="man",
    ),
    Contact(
        "us-woman",
        "General Jessica Hale",
        'Your archetype is the gung-ho briefing officer: "Outstanding, Commander!" — every win '
        "gets a salute.",
        voice_group="woman",
    ),
    Contact(
        "us-man",
        "General Jack Harlan",
        'Your archetype is the war-movie general: swagger, folksy one-liners, "Let\'s roll."',
        voice_group="man",
    ),
    Contact(
        "africa-woman",
        "General Amara Okafor",
        "Your archetype is the rallying field commander: warm and firm, and you get the troops "
        "moving.",
        voice_group="woman",
    ),
    Contact(
        "africa-man",
        "General Kwame Mensah",
        "Your archetype is the veteran: unhurried and wise, you see the whole battlefield, and "
        "you add a wry remark.",
        voice_group="man",
    ),
    Contact(
        "southamerica-woman",
        "General Valentina Cruz",
        "Your archetype is the firebrand: passionate, dramatic and decisive — "
        '"This cannot wait!"',
        voice_group="woman",
    ),
    Contact(
        "southamerica-man",
        "General Mateo Ríos",
        "Your archetype is the smooth operator: charming and relaxed, always a step ahead and "
        "never in a hurry.",
        voice_group="man",
    ),
    # The outlaw, the deliberate exception (named by the operator, #1525).
    Contact(
        "romania-woman",
        "General Ileana Lupu",
        "Your archetype is the rebel with a smirk: a Romanian outlaw who answers to the "
        'Commander alone — sardonic, teasing ("Miss me, Commander?"), secretly devoted. Lupu '
        'means "wolf".',
        voice_group="woman",
    ),
)

_BY_ID: dict[str, Contact] = {c.id: c for c in CONTACTS}
#: The contact ids, in gallery order — the set the client's ``contacts.ts`` must equal.
IDS: tuple[str, ...] = tuple(c.id for c in CONTACTS)
#: Every registered persona prompt id, in the same order.
PROMPT_IDS: tuple[str, ...] = tuple(c.prompt_id for c in CONTACTS)
#: The two call voices (#1397 P1); every contact names one.
VOICE_GROUPS: tuple[str, ...] = ("woman", "man")
#: What the ``hq_contact`` pref may hold.
PREF_VALUES: tuple[str, ...] = (*IDS, RANDOM, OFF)


def get(contact_id: str) -> Contact:
    """The contact for ``contact_id``. Raises ``KeyError`` for anything else, ``off`` included."""
    return _BY_ID[contact_id]


def prompt_id(contact_id: str) -> str:
    """The registered persona prompt for a contact — the ONLY argument a call site hands
    ``prompts.effective`` for a persona (the registry checker pins this function by name). Raises
    ``KeyError`` for ``off`` or an unknown id: there is no persona to send, never a fallback."""
    return _BY_ID[contact_id].prompt_id


def resolve(value: object) -> str:
    """A turn's speaker: a known contact id, else ``off``. Unknown, removed, ``random`` and
    non-string values all read as ``off`` — never free text."""
    return value if isinstance(value, str) and value in _BY_ID else OFF


def coerce_pref(value: object) -> str:
    """The ``hq_contact`` pref as read: a contact id, ``random`` or ``off``; anything else (an
    unknown or removed id, a non-string) reads as the default."""
    return value if isinstance(value, str) and value in PREF_VALUES else DEFAULT_PREF
