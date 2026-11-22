"""Where everything sits on screen.

The wallpaper (drawn in physical pixels by Pillow) and the live widgets (drawn in Qt's
logical pixels) both read these numbers, so the animated cover lands exactly on top of
the cover baked into the wallpaper. Every size is in "design units": pixels of the
1414x890 mock-up the layout was measured from, scaled by unit().
"""

REF_W, REF_H = 1414, 890

# neon violet / electric blue / pale lilac: used until a cover gives us its own colours
DEFAULT_PALETTE = [(155, 92, 255), (64, 170, 255), (214, 160, 255)]

COVER_Y = 0.448          # cover centre, as a fraction of the screen height
COVER = 216              # cover side
COVER_RADIUS = 32
TITLE_DY, TITLE_SIZE = 219, 31     # below the cover centre
ARTIST_DY, ARTIST_SIZE = 251, 20

STAGE_HALF = 360         # the animated area reaches this far left, right and up (rings ~330)
STAGE_BOTTOM = 192       # ...and only this far down: the title starts at 219

# rectangles: (left, top, width, height)
PLAYER = (-215, 275, 430, 114)     # relative to the cover centre
HEADER = (12, 10, 225, 46)         # right margin, top margin, width, height
LYRICS = (19, 3, 283, 196)         # right margin, top (relative to cover centre), w, h; tall enough for two-line lyrics
LYRICS_COLLAPSED_H = 52


def unit(width, height):
    return min(width / REF_W, height / REF_H)


def cover_centre(left, top, width, height):
    return left + width / 2, top + height * COVER_Y
