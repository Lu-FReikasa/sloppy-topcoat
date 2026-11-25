import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./i18n";
import { App } from "./App";
import "./styles.css";
import "./chat-layout.css";
import "./talk.css";
import "./inbox/inbox.css";
import "./files/files.css";
import "./desktop/desktop.css";
import "./desktop/leitstand.css";
import "./desktop/schreibtisch.css";
import { applyChatList, loadChatList } from "./chat/chatListModel";
import { overlayOf } from "./desktop/state";

applyChatList(loadChatList());
// the overlay of the desktop client: see-through from the first paint (the desktop sets it too)
if (overlayOf(window.location.search)) document.documentElement.classList.add("tc-overlay");
// the herald's corner window: see-through too
if (new URLSearchParams(window.location.search).get("herald") === "1") document.documentElement.classList.add("tc-herald");

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
