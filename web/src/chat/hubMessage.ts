/** The hub started an answer in a chat without a question (the second look found an error in the
 *  last answer): the chat page follows it. The stream event of the hub becomes a window event, so
 *  that the page that shows the chat need not know the stream. */
export const HUB_MESSAGE_EVENT = "topcoat:chat-message";

export interface HubMessage {
  conversationId: string;
  messageId: number;
}

export function parseHubMessage(data: string): HubMessage | null {
  try {
    const p = JSON.parse(data) as {
      conversation_id?: unknown;
      message_id?: unknown;
    };
    if (
      typeof p.conversation_id !== "string" ||
      typeof p.message_id !== "number"
    )
      return null;
    return { conversationId: p.conversation_id, messageId: p.message_id };
  } catch {
    return null;
  }
}

export function announceHubMessage(message: HubMessage): void {
  window.dispatchEvent(
    new CustomEvent<HubMessage>(HUB_MESSAGE_EVENT, { detail: message }),
  );
}
