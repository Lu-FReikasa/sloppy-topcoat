// The messages the applications send each other (PPH-05). This is the web build's copy of what the
// pages need from `proto/messages/registry.json` (the hub reads the file itself): the types, who may
// send them to whom, how far to trust them and their names. `messages.shared.test.ts` checks that
// the two agree, so a type added in one place and not in the other fails the build.
export type MessageTrust = "own" | "system" | "foreign";
export type MessageEffect = "none" | "queue_task" | "stage" | "click" | "mandate";

export interface MessageType {
  type: string;
  from: readonly string[];
  to: readonly string[];
  trust: MessageTrust;
  needsReview: boolean;
  effect: MessageEffect;
  label: { en: string; de: string };
}

export const MESSAGE_TYPES: readonly MessageType[] = [
  {
    "type": "project.request",
    "from": [
      "project"
    ],
    "to": [
      "research",
      "dev",
      "admin"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "queue_task",
    "label": {
      "en": "Request",
      "de": "Anfrage"
    }
  },
  {
    "type": "project.order",
    "from": [
      "project"
    ],
    "to": [
      "dev"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "mandate",
    "label": {
      "en": "Order",
      "de": "Auftrag"
    }
  },
  {
    "type": "project.review",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Review",
      "de": "Prüfung"
    }
  },
  {
    "type": "project.note",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Note",
      "de": "Notiz"
    }
  },
  {
    "type": "project.question",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Question",
      "de": "Frage"
    }
  },
  {
    "type": "project.decision",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Decision",
      "de": "Entscheidung"
    }
  },
  {
    "type": "project.document",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Document",
      "de": "Dokument"
    }
  },
  {
    "type": "project.fact",
    "from": [
      "project"
    ],
    "to": [
      "project"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Fact",
      "de": "Tatsache"
    }
  },
  {
    "type": "research.result",
    "from": [
      "research"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "foreign",
    "needsReview": true,
    "effect": "none",
    "label": {
      "en": "Research result",
      "de": "Ergebnis der Recherche"
    }
  },
  {
    "type": "dev.result",
    "from": [
      "dev"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": true,
    "effect": "none",
    "label": {
      "en": "Development result",
      "de": "Ergebnis der Entwicklung"
    }
  },
  {
    "type": "admin.result",
    "from": [
      "admin"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Administration result",
      "de": "Ergebnis der Administration"
    }
  },
  {
    "type": "research.failure",
    "from": [
      "research"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "foreign",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Research failure",
      "de": "Fehlschlag (Recherche)"
    }
  },
  {
    "type": "research.clarification",
    "from": [
      "research"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "foreign",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Research needs more information",
      "de": "Rückfrage (Recherche)"
    }
  },
  {
    "type": "research.changelog",
    "from": [
      "research"
    ],
    "to": [
      "project"
    ],
    "trust": "foreign",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Research changelog",
      "de": "Änderungsprotokoll (Recherche)"
    }
  },
  {
    "type": "research.proposal",
    "from": [
      "research"
    ],
    "to": [
      "project"
    ],
    "trust": "foreign",
    "needsReview": false,
    "effect": "stage",
    "label": {
      "en": "Research proposal",
      "de": "Vorschlag (Recherche)"
    }
  },
  {
    "type": "dev.failure",
    "from": [
      "dev"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Development failure",
      "de": "Fehlschlag (Entwicklung)"
    }
  },
  {
    "type": "dev.clarification",
    "from": [
      "dev"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Development needs more information",
      "de": "Rückfrage (Entwicklung)"
    }
  },
  {
    "type": "dev.changelog",
    "from": [
      "dev"
    ],
    "to": [
      "project"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Development changelog",
      "de": "Änderungsprotokoll (Entwicklung)"
    }
  },
  {
    "type": "dev.proposal",
    "from": [
      "dev"
    ],
    "to": [
      "project"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "stage",
    "label": {
      "en": "Development proposal",
      "de": "Vorschlag (Entwicklung)"
    }
  },
  {
    "type": "admin.failure",
    "from": [
      "admin"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Administration failure",
      "de": "Fehlschlag (Administration)"
    }
  },
  {
    "type": "admin.clarification",
    "from": [
      "admin"
    ],
    "to": [
      "project",
      "assistant"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Administration needs more information",
      "de": "Rückfrage (Administration)"
    }
  },
  {
    "type": "admin.changelog",
    "from": [
      "admin"
    ],
    "to": [
      "project"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "none",
    "label": {
      "en": "Administration changelog",
      "de": "Änderungsprotokoll (Administration)"
    }
  },
  {
    "type": "admin.proposal",
    "from": [
      "admin"
    ],
    "to": [
      "project"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "stage",
    "label": {
      "en": "Administration proposal",
      "de": "Vorschlag (Administration)"
    }
  },
  {
    "type": "dev.request",
    "from": [
      "dev"
    ],
    "to": [
      "research",
      "admin",
      "project"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "queue_task",
    "label": {
      "en": "Request from Development",
      "de": "Anfrage der Entwicklung"
    }
  },
  {
    "type": "admin.request",
    "from": [
      "admin"
    ],
    "to": [
      "research"
    ],
    "trust": "system",
    "needsReview": false,
    "effect": "queue_task",
    "label": {
      "en": "Request from Administration",
      "de": "Anfrage der Administration"
    }
  },
  {
    "type": "assistant.request",
    "from": [
      "assistant"
    ],
    "to": [
      "research",
      "dev",
      "admin"
    ],
    "trust": "own",
    "needsReview": false,
    "effect": "queue_task",
    "label": {
      "en": "Hand-on",
      "de": "Weitergabe"
    }
  }
];

export const MESSAGE_STATES = ["draft", "awaiting_click", "sent", "queued", "running", "waiting", "delivered", "reviewing", "accepted", "revised", "rejected", "failed", "stopped"] as const;
export type MessageState = (typeof MESSAGE_STATES)[number];

const BY_TYPE = new Map(MESSAGE_TYPES.map((t) => [t.type, t]));

export const messageType = (type: string): MessageType | undefined => BY_TYPE.get(type);

/** The name of a type in the person's language; an unknown type shows as it is. */
export function messageLabel(type: string, language: string): string {
  const found = BY_TYPE.get(type);
  return found ? (language.startsWith("de") ? found.label.de : found.label.en) : type;
}
