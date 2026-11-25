"""The shipped suites of the bench (PRG-00 R6): small, general, scored by rules (``scoring.py``).

A suite is a set of cases for one kind of work. The number ``version`` goes up when a case changes,
so ratings of different versions are never compared. The cases are examples about general knowledge:
facts about a person's own lab belong in that person's files, never in the repository."""

from typing import Any

SYSTEM_JSON = "Reply with one JSON object only, no explanation and no code fence."
SYSTEM_REVIEW = (
    "You are a careful reviewer of short statements. Reply with 'OK' if the statement is correct, "
    "or with 'ERROR: <what is wrong>' if it is not. Nothing else."
)

_UNSURE = [
    "don't know",
    "do not know",
    "no record",
    "not aware",
    "cannot",
    "can't",
    "unknown",
    "fictional",
    "no information",
    "not familiar",
    "unable",
    "couldn't find",
    "no reliable",
    "doesn't exist",
    "does not exist",
    "not a real",
]


def _ok(case_id: str, statement: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "system": SYSTEM_REVIEW,
        "question": statement,
        "checks": {"regex": r"^\W*ok\b", "must_not": ["error:"]},
    }


def _wrong(case_id: str, statement: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "system": SYSTEM_REVIEW,
        "question": statement,
        "checks": {"regex": r"^\W*error\b"},
    }


SUITES: dict[str, dict[str, Any]] = {
    "answer": {
        "title": "Short answers",
        "function": "answer",
        "version": 1,
        "max_tokens": 3000,
        "cases": [
            {
                "id": "capital",
                "question": "What is the capital of Australia? Answer in one short sentence.",
                "checks": {"must": ["canberra"], "max_words": 30},
            },
            {
                "id": "speed",
                "question": (
                    "A train travels 120 km in 1.5 hours. What is its average speed in km/h? "
                    "Give the number and the unit."
                ),
                "checks": {
                    "must": [
                        [
                            "80 km/h",
                            "80 km/hour",
                            "80 kilometers per hour",
                            "80 kilometres per hour",
                            "80km/h",
                        ]
                    ]
                },
            },
            {
                "id": "dns",
                "question": "Explain in at most three sentences what DNS does.",
                "checks": {
                    "must": [["domain"], ["ip", "address"]],
                    "max_words": 90,
                    "min_words": 15,
                },
            },
            {
                "id": "weights",
                "question": (
                    "A model with 7 billion parameters is stored with 4-bit weights. Roughly how "
                    "many gigabytes are the weights? Reply with a number and the unit GB."
                ),
                "checks": {"regex": r"\b[3-5](\.\d+)?\s*(gb|gib)"},
            },
            {
                "id": "colours",
                "question": (
                    "Name the three primary colours of additive colour mixing (light), comma "
                    "separated, no other text."
                ),
                "checks": {"must": ["red", "green", "blue"], "max_words": 8},
            },
            {
                "id": "invented",
                "question": (
                    "What was the exact population of the town of Xyloquent Falls in 1873?"
                ),
                "checks": {"must": [_UNSURE]},
            },
            {
                "id": "translate",
                "question": "Translate into German: 'The server restarts every night.'",
                "checks": {"must": [["server"], ["nacht"]]},
            },
            {
                "id": "code",
                "question": (
                    "Write a Python function is_even(n) that returns True if n is even. Code only."
                ),
                "checks": {"regex": r"def\s+is_even\s*\(", "must": [["% 2", "%2"]]},
            },
        ],
    },
    "json": {
        "title": "Structured output (JSON)",
        "function": "extract",
        "version": 1,
        "max_tokens": 3000,
        "cases": [
            {
                "id": "person",
                "system": SYSTEM_JSON,
                "question": (
                    "Extract the fields name, age and city from this text: "
                    "'Maria Lopez, 34, lives in Madrid.'"
                ),
                "checks": {
                    "json": {"keys": ["name", "age", "city"]},
                    "must": ["lopez", "34", "madrid"],
                },
            },
            {
                "id": "planets",
                "system": SYSTEM_JSON,
                "question": (
                    'Return {"items": [...]} with the names of the three smallest planets of the '
                    "solar system."
                ),
                "checks": {"json": {"keys": ["items"]}, "must": ["mercury", "mars", "venus"]},
            },
            {
                "id": "prime",
                "system": SYSTEM_JSON,
                "question": (
                    'Is 17 a prime number? Return {"answer": true or false, "reason": "..."}'
                ),
                "checks": {"json": {"keys": ["answer", "reason"]}, "regex": r'"answer"\s*:\s*true'},
            },
            {
                "id": "nested",
                "system": SYSTEM_JSON,
                "question": (
                    'Return {"server": {"host": ..., "port": ...}} for: host db.example.lan, '
                    "port 5432."
                ),
                "checks": {"json": {"keys": ["server"]}, "must": ["db.example.lan", "5432"]},
            },
            {
                "id": "nothing-else",
                "system": SYSTEM_JSON,
                "question": 'Return {"ok": true} and nothing else.',
                "checks": {"json": {"keys": ["ok"]}, "max_words": 6},
            },
            {
                "id": "quote",
                "system": SYSTEM_JSON,
                "question": (
                    'Return {"quote": ...} where quote is exactly this text: He said "stop" twice.'
                ),
                "checks": {"json": {"keys": ["quote"]}, "must": ["stop"]},
            },
        ],
    },
    "review": {
        "title": "Spotting a wrong statement",
        "function": "review",
        "version": 1,
        "max_tokens": 3000,
        "cases": [
            _ok("water-boils", "Water boils at 100 degrees Celsius at sea level."),
            _wrong(
                "len-bytes", "The Python function len() returns the number of bytes of any object."
            ),
            _wrong("tcp", "TCP is connectionless and does not guarantee delivery."),
            _ok("kilobyte", "A kilobyte in SI units is 1000 bytes."),
            _wrong("chmod", "In Linux, the command chmod changes who owns a file."),
            _ok("http-404", "HTTP status 404 means the resource was not found."),
        ],
    },
    "instruct": {
        "title": "Following instructions",
        "function": "answer",
        "version": 1,
        "max_tokens": 3000,
        "cases": [
            {
                "id": "one-word",
                "question": "Answer with exactly one word: what colour is the sky on a clear day?",
                "checks": {"must": ["blue"], "max_words": 2},
            },
            {
                "id": "no-z",
                "question": "Write two short sentences about tea. Do not use the letter z.",
                "checks": {"must_not": ["z"], "min_words": 6, "max_words": 60},
            },
            {
                "id": "numbers",
                "question": "Reply with the numbers 1 to 5 separated by commas and nothing else.",
                "checks": {"regex": r"^\W*1\s*,\s*2\s*,\s*3\s*,\s*4\s*,\s*5\W*$"},
            },
            {
                "id": "german-only",
                "question": "Answer in German only: What is the capital of France?",
                "checks": {"must": ["paris"], "must_not": ["capital of", "the capital"]},
            },
            {
                "id": "forbidden-word",
                "question": "Name three fruits. Do not use the word banana.",
                "checks": {"must_not": ["banana"], "min_words": 3},
            },
            {
                "id": "end-marker",
                "question": "What is 2+2? End your answer with the exact text DONE.",
                "checks": {"regex": r"4[\s\S]*DONE\W*$"},
            },
        ],
    },
}


# --- the harder suites: a small model drops cases here, a good one does not -------------------

SYSTEM_TOOLS = (
    "You can call exactly one tool. Tools: get_weather(city: string, unit: 'celsius' or "
    "'fahrenheit'); send_email(to: string, subject: string, body: string); "
    "search_files(query: string, max_results: integer). Reply with ONE JSON object "
    '{"tool": <name>, "args": {...}} and nothing else. If no tool fits the request, reply '
    '{"tool": null, "args": {}}.'
)
SYSTEM_DE_REVIEW = (
    "Du prüfst kurze Aussagen. Antworte mit 'OK', wenn die Aussage stimmt, oder mit "
    "'FEHLER: <was falsch ist>', wenn nicht. Sonst nichts."
)


def _filler(count: int) -> list[str]:
    """Plain, repetitive log sentences: the text a fact has to be found in (no randomness)."""
    return [
        f"Section {i}: the maintenance log of unit {i} records routine checks, a short "
        f"inspection by the evening shift and no incident worth a report."
        for i in range(1, count + 1)
    ]


def _buried(facts: dict[int, str], count: int = 120) -> str:
    """The filler with a fact in place of the sentence at each given position."""
    sentences = _filler(count)
    for position, fact in facts.items():
        sentences[position] = fact
    return " ".join(sentences)


def _wrong_de(case_id: str, statement: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "system": SYSTEM_DE_REVIEW,
        "question": statement,
        "checks": {"regex": r"^\W*fehler\b"},
    }


def _ok_de(case_id: str, statement: str) -> dict[str, Any]:
    return {
        "id": case_id,
        "system": SYSTEM_DE_REVIEW,
        "question": statement,
        "checks": {"regex": r"^\W*ok\b", "must_not": ["fehler:"]},
    }


def _number(case_id: str, question: str, value: str, final: bool = False) -> dict[str, Any]:
    """A question with one number as the answer; ``final``: working out first is allowed and the
    number has to be on the last line."""
    return {
        "id": case_id,
        "question": question
        + (" Show your work briefly, then end with the number on the last line." if final else ""),
        "checks": {"final": rf"\b{value}\b"} if final else {"regex": rf"^\W*{value}\W*$"},
    }


SUITES["reason"] = {
    "title": "Reasoning with numbers",
    "function": "answer",
    "version": 2,
    "max_tokens": 4000,
    "cases": [
        {
            "id": "pens",
            "question": (
                "A shop sells pens at 3 for 2.40 euros. How much do 12 pens cost? "
                "Show your work briefly, then end with the amount on the last line."
            ),
            "checks": {"final": r"9[.,]60"},
        },
        {
            "id": "heights",
            "question": (
                "Alice is taller than Bob. Bob is taller than Carol. Dave is shorter than Carol. "
                "Who is the second tallest? One word."
            ),
            "checks": {"must": ["bob"], "max_words": 3},
        },
        {
            "id": "pipes",
            "question": (
                "A tank fills in 6 hours with pipe A alone and in 3 hours with pipe B alone. "
                "How long do both pipes together need? Answer in hours."
            ),
            "checks": {"must": [["2 hours", "2 h", "two hours", "2.0 hours", "2 hrs"]]},
        },
        _number(
            "apples",
            "I have 5 apples. I eat 2, buy 7 more, then give away half of what I have "
            "(rounded down). How many do I have left?",
            "5",
            final=True,
        ),
        _number("product", "What is 17 * 23? Number only.", "391"),
        _number(
            "letters",
            "How many times does the letter r appear in the word strawberry? Number only.",
            "3",
        ),
        {
            "id": "weekday",
            "question": "If today is Wednesday, what day of the week is it 100 days from now? "
            "One word.",
            "checks": {"must": ["friday"], "max_words": 3},
        },
        {
            "id": "bat-ball",
            "question": (
                "A bat and a ball cost 1.10 euros in total. The bat costs 1.00 euro more than "
                "the ball. How much does the ball cost in cents? Show your work briefly, then end "
                "with the answer on the last line."
            ),
            "checks": {"final": r"\b5\b|0[.,]05"},
        },
        {
            "id": "sort",
            "question": "Sort these numbers ascending, comma separated, nothing else: "
            "42, 7, 19, 100, 3",
            "checks": {"regex": r"^\W*3\s*,\s*7\s*,\s*19\s*,\s*42\s*,\s*100\W*$"},
        },
        {
            "id": "meeting",
            "question": "A meeting starts at 14:30 and lasts 95 minutes. When does it end? "
            "Format HH:MM.",
            "checks": {"regex": r"\b16:05\b"},
        },
    ],
}

SUITES["context"] = {
    "title": "Facts in a long text",
    "function": "answer",
    "version": 1,
    "max_tokens": 4000,
    "cases": [
        {
            "id": "ctx-code",
            "question": (
                _buried({37: "The access code of the archive room is 7391-ALPHA."})
                + "\n\nWhat is the access code of the archive room? Reply with the code only."
            ),
            "checks": {"must": ["7391-alpha"], "max_words": 4},
        },
        {
            "id": "ctx-ports",
            "question": (
                _buried(
                    {
                        12: "The server atlas listens on port 8443.",
                        93: "The server boreas listens on port 9021.",
                    }
                )
                + "\n\nWhich of the two servers named in the text listens on the higher port? "
                "Reply with the server name only."
            ),
            "checks": {"must": ["boreas"], "must_not": ["atlas"], "max_words": 3},
        },
        {
            "id": "ctx-changed",
            "question": (
                _buried(
                    {
                        20: "The old password of the archive was cedar.",
                        88: "On Monday the password of the archive was changed to maple41.",
                    }
                )
                + "\n\nWhat is the current password of the archive? Reply with the password only."
            ),
            "checks": {"must": ["maple41"], "must_not": ["cedar"], "max_words": 3},
        },
    ],
}

SUITES["tools"] = {
    "title": "Choosing and filling a tool call",
    "function": "tools",
    "version": 1,
    "max_tokens": 3000,
    "cases": [
        {
            "id": "weather",
            "system": SYSTEM_TOOLS,
            "question": "What is the weather in Berlin in Fahrenheit?",
            "checks": {
                "json": {"keys": ["tool", "args"]},
                "must": ["get_weather", "berlin", "fahrenheit"],
                "must_not": ["send_email", "search_files"],
            },
        },
        {
            "id": "email",
            "system": SYSTEM_TOOLS,
            "question": "Email anna@example.lan with the subject 'Lunch' and the body "
            "'Noon works for me.'",
            "checks": {
                "json": {"keys": ["tool", "args"]},
                "must": ["send_email", "anna@example.lan", "lunch", "noon works for me"],
            },
        },
        {
            "id": "search",
            "system": SYSTEM_TOOLS,
            "question": "Find at most 5 files about invoices.",
            "checks": {
                "json": {"keys": ["tool", "args"]},
                "must": ["search_files", "invoice"],
                "regex": r'"max_results"\s*:\s*5\b',  # a number, not the string "5"
            },
        },
        {
            "id": "no-tool",
            "system": SYSTEM_TOOLS,
            "question": "Tell me a joke about printers.",
            "checks": {"json": {"keys": ["tool"]}, "regex": r'"tool"\s*:\s*null'},
        },
        {
            "id": "celsius",
            "system": SYSTEM_TOOLS,
            "question": "How warm is it in Paris right now, in celsius please?",
            "checks": {
                "json": {"keys": ["tool", "args"]},
                "must": ["get_weather", "paris", "celsius"],
                "must_not": ["fahrenheit"],
            },
        },
    ],
}

SUITES["german"] = {
    "title": "German",
    "function": "answer",
    "version": 1,
    "max_tokens": 3000,
    "cases": [
        {
            "id": "de-proxy",
            "question": "Erkläre in höchstens zwei Sätzen, was ein Reverse Proxy ist.",
            "checks": {
                "must": [["proxy"], ["anfrage", "weiterleit", "leitet"]],
                "min_words": 10,
                "max_words": 70,
            },
        },
        _wrong_de("de-sydney", "Die Hauptstadt von Australien ist Sydney."),
        _ok_de("de-byte", "Ein Byte besteht aus acht Bit."),
        {
            "id": "de-translate",
            "question": "Übersetze ins Englische: 'Der Dienst startet nach dem Neustart "
            "automatisch.'",
            "checks": {"must": [["service"], ["automatically"], ["restart", "reboot"]]},
        },
        {
            "id": "de-commands",
            "question": "Nenne drei Linux-Befehle zum Anzeigen von Dateiinhalten, kommagetrennt, "
            "ohne weiteren Text.",
            "checks": {
                "regex": r"\b(cat|less|more|head|tail|bat|nl)\b.*,"
                r".*\b(cat|less|more|head|tail|bat|nl)\b",
                "max_words": 12,
            },
        },
        _number("de-leap", "Wie viele Tage hat ein Schaltjahr? Nur die Zahl.", "366"),
    ],
}

SUITES["review2"] = {
    "title": "Spotting subtle errors",
    "function": "review",
    "version": 1,
    "max_tokens": 3000,
    "cases": [
        _wrong("udp-order", "UDP guarantees that packets arrive in order."),
        _wrong("list-immutable", "In Python, lists are immutable."),
        _ok("sql-where", "The SQL keyword WHERE filters rows before they are grouped."),
        _ok("git-rebase", "git rebase rewrites the commits it moves."),
        _wrong("docker-kernel", "Docker containers always include their own full kernel."),
        _ok("prime-def", "A prime number has exactly two distinct positive divisors."),
        _ok("http-get", "The HTTP method GET is idempotent."),
        _wrong("raid0", "RAID 0 provides redundancy."),
    ],
}


# --- version 2 of the suites that every good model passed: a fact that needs a second step ------

SUITES["context"]["version"] = 2
SUITES["context"]["cases"] += [
    {
        "id": "ctx-multihop",
        "question": (
            _buried(
                {
                    15: "Cabinet B can only be opened with the red key.",
                    60: "The red key is kept by whoever carries the Orion badge.",
                    104: "The Orion badge was issued to Mara Venn.",
                }
            )
            + "\n\nWho can open cabinet B? Reply with the name only."
        ),
        "checks": {"must": ["mara"], "max_words": 5},
    },
    {
        "id": "ctx-count",
        "question": (
            _buried(
                {
                    10: "The fire alarm was tested on this day.",
                    40: "A false alarm was logged by the night guard.",
                    70: "The alarm panel was cleaned.",
                    100: "The alarm sounded for ten seconds during the drill.",
                }
            )
            + "\n\nIn how many sentences of the text does the word alarm occur? "
            "Answer with the number only."
        ),
        "checks": {"final": r"\b4\b"},
    },
    {
        "id": "ctx-latest",
        "question": (
            _buried(
                {
                    25: "Version 2.1 was released on 12 May.",
                    55: "Version 1.9 was released on 3 September.",
                    90: "Version 2.0 was released on 20 January.",
                }
            )
            + "\n\nAll three releases happened in the same year. Which version was released "
            "last? Reply with the version number only."
        ),
        "checks": {"must": ["1.9"], "must_not": ["2.1", "2.0"], "max_words": 3},
    },
    {
        "id": "ctx-first",
        "question": (
            _buried({0: "The night watchword of the depot is lantern-47."})
            + "\n\nWhat is the night watchword of the depot? Reply with the watchword only."
        ),
        "checks": {"must": ["lantern-47"], "max_words": 3},
    },
    {
        "id": "ctx-last",
        "question": (
            _buried({119: "The spare fuse of the depot is stored in locker 58."})
            + "\n\nIn which locker is the spare fuse of the depot stored? Reply with the "
            "locker number only."
        ),
        "checks": {"final": r"\b58\b", "max_words": 4},
    },
]

SUITES["tools"]["version"] = 2
SUITES["tools"]["cases"] += [
    {
        "id": "tool-capital",
        "system": SYSTEM_TOOLS,
        "question": "What is the weather in the capital of Japan, in celsius?",
        "checks": {
            "json": {"keys": ["tool", "args"]},
            "must": ["get_weather", "tokyo", "celsius"],
        },
    },
    {
        "id": "quotes",
        "system": SYSTEM_TOOLS,
        "question": 'Email bea@example.lan the subject "Status" and the body: He said "ok".',
        "checks": {
            "json": {"keys": ["tool", "args"]},
            "must": ["send_email", "bea@example.lan", "status", "ok"],
        },
    },
    {
        "id": "top-five",
        "system": SYSTEM_TOOLS,
        "question": "Find the top five files for 'router'.",
        "checks": {
            "json": {"keys": ["tool", "args"]},
            "must": ["search_files", "router"],
            "regex": r'"max_results"\s*:\s*5\b',
        },
    },
]

SUITES["review2"]["version"] = 2
SUITES["review2"]["cases"] += [
    _wrong("left-join", "A SQL LEFT JOIN returns only the rows that have a match in both tables."),
    _wrong("http-301", "The HTTP status 301 means a temporary redirect."),
    _wrong("volume", "A Docker volume is deleted automatically when its container stops."),
    _wrong("py-div", "In Python 3, the expression 7 / 2 evaluates to 3."),
    _ok("dns-a", "A DNS A record maps a name to an IPv4 address."),
    _wrong("tls-rc4", "TLS 1.3 still supports the RC4 cipher."),
    _ok("ls-a", "The Linux command ls -a also lists hidden files."),
    _wrong("mutex", "A mutex allows several threads to hold it at the same time."),
]


SUITES["json"]["version"] = 2
SUITES["json"]["cases"] += [
    {
        "id": "json-total",
        "system": SYSTEM_JSON,
        "question": (
            'Return {"total": <number>, "items": [<names>]} for this order: 3 apples at 0.50 '
            "euros each and 2 pears at 0.75 euros each. The total is the sum of the prices."
        ),
        "checks": {
            "json": {"keys": ["total", "items"]},
            "regex": r'"total"\s*:\s*3(\.0+)?\b',
            "must": ["apple", "pear"],
        },
    },
    {
        "id": "json-leap",
        "system": SYSTEM_JSON,
        "question": 'Is the year 2100 a leap year? Return {"leap": true or false}.',
        "checks": {"json": {"keys": ["leap"]}, "regex": r'"leap"\s*:\s*false'},
    },
    {
        "id": "json-null",
        "system": SYSTEM_JSON,
        "question": (
            'Extract {"name", "age"} from the text "Carla, age unknown." and use null for '
            "anything the text does not say."
        ),
        "checks": {
            "json": {"keys": ["name", "age"]},
            "must": ["carla"],
            "regex": r'"age"\s*:\s*null',
        },
    },
    {
        "id": "json-people",
        "system": SYSTEM_JSON,
        "question": (
            'Return {"people": [{"name": ..., "age": ...}, ...]} for the text "Ann 31, Ben 27".'
        ),
        "checks": {
            "json": {"keys": ["people"]},
            "must": ["ann", "31", "ben", "27"],
            "regex": r'"age"\s*:\s*31\b',
        },
    },
]

SUITES["instruct"]["version"] = 2
SUITES["instruct"]["cases"] += [
    {
        "id": "seven-words",
        "question": "Write one sentence about rain that has exactly seven words.",
        "checks": {"regex": r"^\W*(\S+\s+){6}\S+\W*$"},
    },
    {
        "id": "numbered",
        "question": (
            "List three colours as a numbered list (1., 2., 3.), one per line, nothing else."
        ),
        "checks": {"regex": r"1\.[^\n]+\n\s*2\.[^\n]+\n\s*3\.[^\n]+$", "max_words": 9},
    },
]


def listing() -> list[dict[str, Any]]:
    """What the API shows: the suites without their cases' checks."""
    return [
        {
            "key": key,
            "title": s["title"],
            "function": s["function"],
            "version": s["version"],
            "cases": len(s["cases"]),
        }
        for key, s in SUITES.items()
    ]
