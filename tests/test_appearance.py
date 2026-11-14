import numpy as np

from vision.appearance import appearance_distance, clothing_descriptor


def person(shirt_bgr, pants_bgr, head_bgr=(40, 80, 200), h=200, w=80):
    img = np.zeros((h + 20, w + 20, 3), np.uint8)
    img[10:10 + int(0.2 * h), 10:10 + w] = head_bgr          # head band (must be ignored)
    img[10 + int(0.2 * h):10 + int(0.55 * h), 10:10 + w] = shirt_bgr
    img[10 + int(0.55 * h):10 + h, 10:10 + w] = pants_bgr
    return img, (10, 10, 10 + w, 10 + h)


def test_head_region_does_not_change_the_descriptor():
    a, box = person((0, 0, 200), (120, 60, 20), head_bgr=(40, 80, 200))
    b, _ = person((0, 0, 200), (120, 60, 20), head_bgr=(200, 200, 200))
    assert appearance_distance(clothing_descriptor(a, box), clothing_descriptor(b, box)) == 0.0


def test_distance_separates_different_clothes_and_is_symmetric():
    red, box = person((0, 0, 200), (120, 60, 20))
    red2, _ = person((0, 0, 205), (118, 62, 22))
    green, _ = person((0, 180, 0), (30, 30, 30))
    d_same = appearance_distance(clothing_descriptor(red, box), clothing_descriptor(red2, box))
    d_diff = appearance_distance(clothing_descriptor(red, box), clothing_descriptor(green, box))
    assert d_same < 0.2 < 0.6 < d_diff <= 1.0
    assert d_diff == appearance_distance(clothing_descriptor(green, box), clothing_descriptor(red, box))


def test_too_small_box_has_no_descriptor():
    img, _ = person((0, 0, 200), (120, 60, 20))
    assert clothing_descriptor(img, (10, 10, 18, 30)) is None
