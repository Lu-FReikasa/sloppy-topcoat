"""Images in the chat (D125): re-encoded, stored per person, sent only to a model that sees."""

import base64
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import AUTH
from tests.fakenode import online_node
from tests.test_chat import ask, chat_requests, new_conv, sse_events, use_model
from tests.users import make_user
from topcoat_hub import chat_images
from topcoat_hub.chat_images import Seer, choose, expand, pick_for_prompt


def picture(
    size: tuple[int, int] = (64, 48), fmt: str = "JPEG", mode: str = "RGB", **save: Any
) -> bytes:
    img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 0))
    out = io.BytesIO()
    img.save(out, fmt, **save)
    return out.getvalue()


def with_location() -> bytes:
    """A JPEG with EXIF: a GPS position, a camera serial and the orientation "turned 90°"."""
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: the camera was turned, the picture must be rotated
    exif[0xA431] = "SERIAL-1234"  # BodySerialNumber
    exif[0x8825] = {1: "N", 2: (52.0, 31.0, 12.0), 3: "E", 4: (13.0, 24.0, 18.0)}  # GPS
    return picture((80, 40), exif=exif.tobytes(), comment=b"private note")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --- re-encoding ---------------------------------------------------------------------------------


def test_reencoding_drops_every_metadata_and_turns_the_picture_upright() -> None:
    raw = with_location()
    assert b"SERIAL-1234" in raw and b"private note" in raw
    out = chat_images.reencode(raw)
    assert out.content_type == "image/jpeg"
    assert b"SERIAL-1234" not in out.data and b"private note" not in out.data
    with Image.open(io.BytesIO(out.data)) as img:
        assert img.format == "JPEG"
        assert not img.getexif()  # no EXIF at all, the GPS block with it
        assert "icc_profile" not in img.info and "comment" not in img.info
        assert img.size == (40, 80) == (out.width, out.height)  # rotated by its orientation


def test_large_pictures_are_scaled_within_the_limits() -> None:
    noisy = Image.effect_noise((4000, 3000), 120).convert("RGB")  # does not compress well
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    out = chat_images.reencode(buf.getvalue())
    assert max(out.width, out.height) <= chat_images.MAX_SIDE
    assert len(out.data) <= chat_images.MAX_STORED_BYTES
    assert out.width / out.height == pytest.approx(4 / 3, rel=0.01)


def test_transparency_is_flattened_on_white() -> None:
    out = chat_images.reencode(picture(fmt="PNG", mode="RGBA"))
    with Image.open(io.BytesIO(out.data)) as img:
        r, g, b = img.convert("RGB").getpixel((5, 5))  # type: ignore[misc]
        assert min(r, g, b) > 240


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not a picture", "bad_image"),
        (b"%PDF-1.4 ...", "bad_image"),
        (picture(fmt="TIFF"), "bad_image"),
    ],
)
def test_what_is_not_an_allowed_picture_is_refused(raw: bytes, code: str) -> None:
    with pytest.raises(chat_images.ImageError) as err:
        chat_images.reencode(raw)
    assert err.value.code == code


def test_uploads_are_base64_or_data_urls_within_the_size_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = picture()
    assert chat_images.decode_upload(b64(raw)) == raw
    assert chat_images.decode_upload(f"data:image/jpeg;base64,{b64(raw)}") == raw
    with pytest.raises(chat_images.ImageError):
        chat_images.decode_upload("***")
    monkeypatch.setattr(chat_images, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(chat_images.ImageError) as err:
        chat_images.decode_upload(b64(raw))
    assert err.value.code == "image_too_large"


# --- which model sees ----------------------------------------------------------------------------


def test_the_model_of_the_answer_keeps_the_image_where_it_sees() -> None:
    found = [Seer("node-a", "eye-vl"), Seer("node-b", "other-vl", house=True)]
    sight = choose(found, "eye-vl", picked=False)
    # pinned to the node with the projector: the same model elsewhere would be blind
    assert (sight.sees, sight.requested, sight.routed_from) == (True, "eye-vl@node-a", None)
    # a pin of the person to a node where it is blind is not widened
    assert not choose(found, "eye-vl@node-c", picked=True).sees


def test_a_blind_answer_goes_to_a_house_model_that_sees_first() -> None:
    found = [
        Seer("node-w", "tiny-vl", house=True, worker=True),
        Seer("node-a", "loose-vl"),
        Seer("node-b", "house-vl", house=True),
    ]
    sight = choose(found, "small", picked=False)
    assert (sight.sees, sight.model, sight.requested) == (True, "house-vl", "house-vl@node-b")
    assert sight.routed_from == "small" and sight.node == "node-b"
    # without a house model: the first loaded one that sees
    assert choose(found[1:2], "small", picked=False).requested == "loose-vl@node-a"


def test_a_picked_blind_model_and_no_seeing_model_mean_nobody_sees() -> None:
    assert choose([Seer("node-a", "eye-vl")], "small", picked=True) == chat_images.Sight(
        False, "small", "small"
    )
    assert not choose([], "small", picked=False).sees


def test_images_stand_in_the_prompt_where_the_person_put_them() -> None:
    a, b = "a" * 32, "b" * 32
    messages = [
        {"role": "system", "content": f"rules {chat_images.marker(a)}"},
        {"role": "user", "content": "old" + chat_images.marked([b])},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "what is this?" + chat_images.marked([a])},
    ]
    out, sent = expand(messages, {a: ("image/jpeg", b"\xff\xd8x")})
    assert sent == 1
    assert out[0] == messages[0]  # only user turns carry images
    assert out[1]["content"] == [
        {"type": "text", "text": "old\n\n"},
        {"type": "text", "text": chat_images.EARLIER_LINE},
    ]
    assert out[3]["content"][0] == {"type": "text", "text": "what is this?\n\n"}
    assert out[3]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,/9h4"


def test_the_newest_images_go_first_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_images, "PROMPT_IMAGE_BYTES", 100)
    assert pick_for_prompt([("new", 90), ("mid", 20), ("old", 5)]) == ["new", "old"]
    assert pick_for_prompt([("huge", 500), ("old", 5)]) == ["huge"]  # the newest always goes
    monkeypatch.setattr(chat_images, "MAX_PROMPT_IMAGES", 1)
    assert pick_for_prompt([("a", 1), ("b", 1)]) == ["a"]


# --- the chat ------------------------------------------------------------------------------------


def send(
    client: TestClient, conv: str, content: str, images: list[bytes], headers: dict[str, str] = AUTH
) -> tuple[int, list[tuple[str, dict[str, Any]]], Any]:
    r = client.post(
        f"/api/conversations/{conv}/messages",
        headers=headers,
        json={"content": content, "images": [b64(i) for i in images]},
    )
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        return r.status_code, sse_events(r.text), r
    return r.status_code, [], r


def image_parts(body: dict[str, Any]) -> list[str]:
    return [
        p["image_url"]["url"]
        for m in body["messages"]
        if isinstance(m["content"], list)
        for p in m["content"]
        if p.get("type") == "image_url"
    ]


def all_text(body: dict[str, Any]) -> str:
    out = []
    for m in body["messages"]:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out += [p["text"] for p in content if p.get("type") == "text"]
    return "\n".join(out)


def test_an_image_goes_to_a_model_that_sees_and_stays_with_its_person(
    client: TestClient, connect, clean_db: str
) -> None:
    node = online_node(client, connect, "node-a", loaded=["eye-vl"])
    use_model(client, "eye-vl")
    conv = new_conv(client)
    status, events, r = send(client, conv, "What is in the picture?", [with_location()])
    assert status == 200, r.text
    meta = events[0][1]
    assert meta["vision"] == {"sees": True, "model": "eye-vl", "images": 1}
    shown = meta["user_message"]["images"]
    assert len(shown) == 1 and (shown[0]["width"], shown[0]["height"]) == (40, 80)

    body = chat_requests(node)[-1]
    urls = image_parts(body)
    assert len(urls) == 1 and urls[0].startswith("data:image/jpeg;base64,")
    sent = base64.b64decode(urls[0].partition(",")[2])
    assert b"SERIAL-1234" not in sent  # the re-encoded picture, not what the browser sent
    assert chat_images.DATA_RULE in body["messages"][0]["content"]
    assert "What is in the picture?" in all_text(body)
    assert chat_images.HIDDEN_LINE not in all_text(body)

    # the conversation lists it; the picture itself only for its person
    got = client.get(f"/api/conversations/{conv}", headers=AUTH).json()
    user_msg = next(m for m in got["messages"] if m["role"] == "user")
    assert [i["id"] for i in user_msg["images"]] == [shown[0]["id"]]
    pic = client.get(f"/api/chat/images/{shown[0]['id']}", headers=AUTH)
    assert pic.status_code == 200 and pic.content == sent
    assert pic.headers["content-type"] == "image/jpeg"
    assert pic.headers["x-content-type-options"] == "nosniff"
    _, other = make_user(clean_db, "other")
    assert client.get(f"/api/chat/images/{shown[0]['id']}", headers=other).status_code == 404

    # a follow-up still shows it (it is part of the conversation)
    status, _, _ = ask(client, conv, "And the colour?")
    assert status == 200
    assert len(image_parts(chat_requests(node)[-1])) == 1

    # deleting the conversation deletes the picture
    assert client.delete(f"/api/conversations/{conv}", headers=AUTH).status_code == 204
    assert client.get(f"/api/chat/images/{shown[0]['id']}", headers=AUTH).status_code == 404


def test_a_model_that_cannot_see_never_gets_the_image_and_the_chat_says_so(
    client: TestClient, connect
) -> None:
    node = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    conv = new_conv(client)
    status, events, r = send(client, conv, "Read this", [picture()])
    assert status == 200, r.text
    meta = events[0][1]
    assert meta["vision"] == {"sees": False, "model": "small", "images": 0}
    note = next(d["item"] for e, d in events if e == "activity")
    assert (note["kind"], note["data"]["event"], note["title"]) == ("note", "vision_blind", "small")

    body = chat_requests(node)[-1]
    assert image_parts(body) == []
    assert "base64" not in all_text(body)
    assert chat_images.HIDDEN_LINE in all_text(body)
    done = events[-1][1]["message"]
    assert done["activity"][0]["data"]["event"] == "vision_blind"  # kept with the answer


def test_an_answer_with_an_image_is_routed_to_a_loaded_model_that_sees(
    client: TestClient, connect
) -> None:
    blind = online_node(client, connect, "node-a", loaded=["small"])
    seeing = online_node(client, connect, "node-b", loaded=["eye-vl"])
    use_model(client)
    conv = new_conv(client)
    status, events, r = send(client, conv, "Look", [picture()])
    assert status == 200, r.text
    meta = events[0][1]
    assert (meta["model"], meta["node"]) == ("eye-vl", "node-b")
    assert meta["vision"] == {
        "sees": True,
        "model": "eye-vl",
        "images": 1,
        "routed_from": "small",
        "node": "node-b",
    }
    note = next(d["item"] for e, d in events if e == "activity")
    assert (note["data"]["event"], note["title"]) == ("vision_routed", "eye-vl @ node-b")
    assert len(image_parts(chat_requests(seeing)[-1])) == 1
    assert chat_requests(blind) == []

    # a text message without images keeps the usual model
    ask_conv = new_conv(client)
    status, events, _ = ask(client, ask_conv, "Hello")
    assert events[0][1]["model"] == "small" and "vision" not in events[0][1]


def test_a_model_the_person_picked_is_not_replaced(client: TestClient, connect) -> None:
    blind = online_node(client, connect, "node-a", loaded=["small"])
    online_node(client, connect, "node-b", loaded=["eye-vl"])
    conv = new_conv(client, model="small", node="node-a")
    status, events, _ = send(client, conv, "Look", [picture()])
    assert status == 200
    assert events[0][1]["vision"]["sees"] is False
    assert image_parts(chat_requests(blind)[-1]) == []


def test_a_message_with_a_broken_image_is_refused_and_not_stored(
    client: TestClient, connect
) -> None:
    online_node(client, connect, "node-a", loaded=["eye-vl"])
    use_model(client, "eye-vl")
    conv = new_conv(client)
    status, _, r = send(client, conv, "Look", [b"this is no picture"])
    assert status == 422 and r.json()["detail"]["code"] == "bad_image"
    assert client.get(f"/api/conversations/{conv}", headers=AUTH).json()["messages"] == []
    # an image alone is a message; nothing at all is not
    status, _, _ = send(client, conv, "", [picture()])
    assert status == 200
    r = client.post(f"/api/conversations/{conv}/messages", headers=AUTH, json={"content": ""})
    assert r.status_code == 422
    too_many = [b64(picture())] * (chat_images.MAX_PER_MESSAGE + 1)
    r = client.post(
        f"/api/conversations/{conv}/messages",
        headers=AUTH,
        json={"content": "x", "images": too_many},
    )
    assert r.status_code == 422


# --- incognito: in memory only (D63) -------------------------------------------------------------


def test_an_incognito_image_is_seen_kept_in_the_tab_and_never_written(
    client: TestClient, connect, clean_db: str
) -> None:
    from tests.test_incognito import new_tab, requests_to_the_model, row_counts

    online_node(client, connect, "node-a", loaded=["small"])
    seeing = online_node(client, connect, "node-b", loaded=["eye-vl"])
    use_model(client)
    tab = new_tab(client, internet=False)
    before = row_counts()
    r = client.post(
        f"/api/incognito/{tab}/messages",
        headers=AUTH,
        json={"content": "What is this?", "images": [b64(with_location())]},
    )
    assert r.status_code == 200, r.text
    assert row_counts() == before, "an incognito image must leave no row in any table"
    body = r.json()
    assert body["message"]["vision"] == {
        "sees": True,
        "model": "eye-vl",
        "images": 1,
        "routed_from": "small",
    }
    image = body["asked"]["images"][0]
    sent = requests_to_the_model(seeing)[-1]
    urls = image_parts(sent)
    assert len(urls) == 1 and chat_images.DATA_RULE in sent["messages"][0]["content"]
    got = client.get(f"/api/incognito/{tab}/images/{image['id']}", headers=AUTH)
    assert got.status_code == 200 and got.content == base64.b64decode(urls[0].partition(",")[2])
    assert b"SERIAL-1234" not in got.content
    _, other = make_user(clean_db, "other")
    assert (
        client.get(f"/api/incognito/{tab}/images/{image['id']}", headers=other).status_code == 404
    )
    # the tab shows it with the message; burning the tab burns the image
    shown = client.get(f"/api/incognito/{tab}", headers=AUTH).json()["messages"][0]
    assert shown["images"] == [image]
    assert client.delete(f"/api/incognito/{tab}", headers=AUTH).status_code == 204
    assert client.get(f"/api/incognito/{tab}/images/{image['id']}", headers=AUTH).status_code == 404


def test_incognito_without_a_seeing_model_keeps_the_image_from_the_model(
    client: TestClient, connect
) -> None:
    from tests.test_incognito import new_tab, requests_to_the_model

    blind = online_node(client, connect, "node-a", loaded=["small"])
    use_model(client)
    tab = new_tab(client, internet=False)
    r = client.post(
        f"/api/incognito/{tab}/messages", headers=AUTH, json={"images": [b64(picture())]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["vision"]["sees"] is False
    sent = requests_to_the_model(blind)[-1]
    assert image_parts(sent) == [] and chat_images.HIDDEN_LINE in all_text(sent)
    r = client.post(
        f"/api/incognito/{tab}/messages", headers=AUTH, json={"content": "x", "images": ["!!"]}
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "bad_image"
