# The desktop: programs instead of pages (D70)

This replaces the shells of the earlier layout plan (D60: Terminal, Schreibtisch, the narrow stack), the five
looks and the theme controls. It is the answer to the maintainer's review of the one-UI release:
one design, a desktop with programs, no menu bar, a command palette, and the settings of an
application inside that application.

## 1. The picture

The whole page is a **desktop**. At the bottom is the **taskbar**: one button per program (open,
minimised or closed), the connection, the person, the clock of nothing else. Open programs are
**tiled automatically**: one fills the desktop, two stand side by side, three are one large and two
small, four and more a grid. One program can be **maximised**; the others wait on the taskbar.
There is no menu bar at the top.

A **program** is a window with a title bar (its name in its colour, minimise, maximise, close), a
narrow **navigation** on the left with the views that belong to it, and the view on the right. A
program's settings that concern the person are one of its views.

| program | for | views |
|---|---|---|
| Chat | everyone | Inbox, Conversation, the chats of every application, Incognito, Hand-overs, Settings, Shelf, Memory |
| Research | everyone | Conversation, Knowledge, Shelf, Memory, Settings |
| Development | admins | Conversation, Workshop, Repositories, Shelf, Memory, Settings |
| Administration | admins | Conversation, Machines (with the inventory), Models (the catalogue, and the table of which program uses which model and machine), Flows, System, Identities, Memory care, Programs & rights (with the API keys), Memory |
| Projects | everyone | Projects, Memory, Settings |
| Incognito | everyone | Incognito |
| Settings | everyone | Me, Instructions, Memory |

A program is a built-in workspace template (`ui_templates`, D60/D61): its `modules` are its views
in order, `open` its first view, `agent` the ceiling of its tools (D69), `requires_role` who sees
it. The admin may still copy and change programs.

## 2. Keys

| key | does |
|---|---|
| `Ctrl+K` | the **command palette**, in the middle: programs, views, settings and actions by name; Enter opens |
| `Ctrl+Enter` | the conversation of the focused program (its chat, in its context) |
| `Alt+1` … `Alt+9` | the n-th program of the taskbar: open, or bring forward |
| `Escape` | close the palette; otherwise minimise the focused program |

A key does nothing while the person types in a field, except `Ctrl+K` and `Escape`.

## 3. State

What is open is the person's own and stored on the hub as part of their interface preferences
(`users.ui_prefs`, `PUT /api/ui/prefs`), so it follows them to another browser. The hub clamps it
like every preference (`topcoat_hub.ui.prefs`): unknown programs and views are dropped, never an
error.

```json
{
  "hue": 140,
  "desktop": {
    "open": ["chat", "inbox"],
    "minimised": [],
    "focus": "chat",
    "maximised": null,
    "views": { "chat": { "module": "page.chat", "props": {} } }
  }
}
```

`open` is the order on the taskbar and in the tiling. `views` holds the view each program shows.
The schema is `proto/ui/prefs.schema.json`.

## 4. Look

Three looks, chosen in the person's menu (`look` in `ui_prefs`, clamped by the hub); the hue applies
to both.

- **Terminal** (the default): dark, one accent from the hue, each program with its own fixed
  colour for its title and its tag in the inbox.
- **Leitstand** (control room; D73): mission control, a cockpit, a little of a spy film's
  operations room. Its rule is the cockpit's: colour is information, never decoration. Green is
  normal, cyan is the selection and advice, amber wants attention, red is a warning; everything
  else is grey legends on dark glass. A program is a display in a bezel with a legend plate (its
  three-letter page code, e.g. `ADM`, and a lamp lit while it has the focus); the taskbar is the
  annunciator panel (an unlit, dark key = closed, its legend still readable grey; green text = open,
  lit green = in front, amber = the inbox has something to read, flashing red = something urgent); a
  clock keeps UTC. The hue tints only the
  housing, never a signal. A display comes on with a short flicker.

- **Schreibtisch** (desk; back from the desk shell of the layout prototypes): programs are slips of
  paper on a cutting mat. Each program has its own paper (inbox a yellow legal pad, Research an index
  card, Development a ledger, Administration a carbon copy, Settings green-bar paper), a slip lies a
  little askew and is held by tape, the one in front lies flat; the taskbar is the desk's edge with a
  ruler. The hue tints the mat.

The inbox's signal (amber/red) is shown in every look; the terminal marks it with a dot. Motion follows the operating system
(`prefers-reduced-motion`). The narrow screen is the same desktop: one program at a time, the
taskbar stays.

## 5. What is removed

- The looks Glas, Kontrollraum, Werkstatt, Terminal, Cyberpunk (`look.ts`, `looks.css`,
  `LookSwitch`, `users.look`, `PUT /api/me/look`).
- The theme controls saturation, density, ornament, motion.
- The shells Terminal (tile), Schreibtisch (desk) and the narrow stack, the per-shell layout
  documents (`ui_layouts`, `ui_templates.layouts`, `/api/ui/layouts`, `/api/ui/templates/{key}/
  layouts`, `proto/ui/layout.schema.json`), the shell choice and the "add window" menu.
- The menu bar with workspaces, shell picker, appearance, look, language, add node, logout: the
  language and logging out are in the person's menu on the taskbar, adding a node is a view of
  Administration and an action of the palette.
- The layout document window (a developer tool).

The agent still opens a view by an intent (`requestOpen({module, props})`, `layout/intent.ts`): the
desktop opens the program that holds the module and shows it there.

## 6. Inside a program

- **Settings** of Chat, Research and Development: what the program may use (its tools are a ceiling,
  D69) and which of the person's tagged notes it may see.
- **Shelf**: the reports and the conversations that were put away, in two tabs.
- **Hand-over**: a question this program cannot answer can be handed to another program, by the
  person (the link in the chat window) or proposed by the assistant (D72); either way the person
  reads the dossier in the inbox and sends it.
- Administration › **System** holds the hub's models (default and second look) and the technical
  settings; how the chat answers (limits, second look, recall, notes, placement, Nachlese, emoji
  answers) is the flow "Chat answer" (D67, D71).

## 7. Kept small on purpose

- A few pages that belong together are one view with tabs (`layout/modules/Tabbed.tsx`): Machines
  with the inventory, Models with the responsibilities, Programs & rights with the API keys. A link
  to such a page opens the view on its tab (`desktop/state.ts`, `TAB_OF`).
- Nothing is locked: the desktop has no arrangement a person could break, so profiles and programs
  carry no "locked" flag any more (migration 0065).
- One look, one source of motion: the desktop's stylesheet stops every animation when the system
  asks for less motion, and `motion.ts` answers the same question for the motion made by script.
