import cv2
import numpy as np
import pytest

from mapping.frame_selection import Cancelled, SelectionConfig, SelectionError, select_frames
from mapping.frame_source import InputError, open_source


def _textured(seed, shift=0):
    rng = np.random.default_rng(seed)
    base = (rng.random((60, 80, 3)) * 255).astype(np.uint8)
    img = cv2.resize(base, (320, 240), interpolation=cv2.INTER_NEAREST)
    return np.roll(img, shift, axis=1)


def _write(folder, images):
    folder.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(images):
        cv2.imwrite(str(folder / f"{i:04d}.png"), im)
    return folder


def test_blurred_and_duplicate_frames_are_rejected(tmp_path):
    imgs = [_textured(1, 12 * i) for i in range(10)]
    imgs[4] = cv2.GaussianBlur(imgs[4], (0, 0), 6)  # motion-blur stand-in
    imgs.insert(7, imgs[6].copy())  # exact duplicate of a kept frame
    src = open_source(_write(tmp_path / "in", imgs))
    res = select_frames(src, tmp_path / "out", SelectionConfig(target_fps=0))
    kept = [k.source_index for k in res.kept]
    assert 4 not in kept and res.rejected_blur == 1
    assert res.rejected_duplicate == 1 and 7 not in kept
    assert len(list((tmp_path / "out").glob("*.jpg"))) == len(kept) == 9


def test_cap_limits_frame_count(tmp_path):
    src = open_source(_write(tmp_path / "in", [_textured(2, 7 * i) for i in range(20)]))
    res = select_frames(src, tmp_path / "out", SelectionConfig(target_fps=0, max_frames=5))
    assert len(res.kept) == 5 and res.dropped_by_cap == 15
    assert res.kept[0].source_index == 0 and res.kept[-1].source_index == 19  # spread over the whole clip


def test_static_or_flat_input_fails_with_actionable_message(tmp_path):
    flat = [np.full((240, 320, 3), 128, np.uint8)] * 5
    with pytest.raises(SelectionError, match="usable frames"):
        select_frames(open_source(_write(tmp_path / "in", flat)), tmp_path / "out", SelectionConfig(target_fps=0))


def test_cancel_stops_scan(tmp_path):
    src = open_source(_write(tmp_path / "in", [_textured(3, i) for i in range(5)]))
    with pytest.raises(Cancelled):
        select_frames(src, tmp_path / "out", SelectionConfig(target_fps=0), should_stop=lambda: True)


def test_video_source_samples_by_fps(tmp_path):
    path = tmp_path / "clip.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (320, 240))
    for i in range(60):
        vw.write(_textured(4, 5 * i))
    vw.release()
    src = open_source(path)
    assert src.kind == "video_file" and src.nominal_fps() == pytest.approx(30, abs=0.5)
    res = select_frames(src, tmp_path / "out", SelectionConfig(target_fps=3))
    assert res.step == 10 and res.candidates == 6


@pytest.mark.parametrize("make,match", [
    (lambda p: p / "missing", "does not exist"),
    (lambda p: (p / "empty").mkdir() or p / "empty", "No .png/.jpg"),
    (lambda p: (p / "x.txt").write_text("hi") and p / "x.txt", "Unsupported input"),
    (lambda p: (p / "bad.mp4").write_bytes(b"not a video") and p / "bad.mp4", "Cannot open video|no decodable"),
])
def test_bad_inputs_raise_input_error(tmp_path, make, match):
    with pytest.raises(InputError, match=match):
        open_source(make(tmp_path))
