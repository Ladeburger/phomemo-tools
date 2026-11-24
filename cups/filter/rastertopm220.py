#! /usr/bin/env python3
#
# rastertopm220 - CUPS raster filter for the Phomemo M220
#
# This is a fork of rastertopm110.py with one fix: on the M220 the
# thermal print head is physically wider than most of the labels that
# are printed on it. The head/sensor assembly is ~72mm (576 dots @
# 203dpi) wide, but labels are commonly 20-50mm wide and are not
# mounted flush with the left edge of the head - they run roughly
# centred under it.
#
# rastertopm110.py writes the CUPS page width (i.e. the selected
# MediaSize, e.g. 38mm/304 dots for "w40h20") directly as the GS v 0
# raster width and starts the bit image at column 0. Since the printer
# has no concept of "page", it just burns dots starting at the left
# edge of the head, this makes every label narrower than the full head
# width print shifted to the left of where the label stock actually
# is - on a 40x20mm label this is a ~17mm horizontal offset.
#
# This was confirmed by comparing a Windows-generated USB capture
# (which always sends the full head width, 576 dots, with the
# label content centred inside it) against a Linux-generated one for
# the exact same label (40x20mm): the Linux raster only contained 304
# dots (the label width) and was emitted at offset 0, while the same
# pixels in the Windows capture started at dot 136 (= (576-304)/2).
#
# The fix below pads every raster line, left and right, so that the
# GS v 0 image is always exactly PRINTHEAD_WIDTH_DOTS wide with the
# real content centred in it, regardless of which MediaSize/label
# width was selected in CUPS.
#
# Copyright (c) 2020 Laurent Vivier <laurent@vivier.eu> (original rastertopm110.py)
# Copyright (c) 2026 (M220 head-centering fix)
#
# Licensed under the GNU General Public License v3, same as the rest
# of phomemo-tools.

import sys, os
from collections import namedtuple
from struct import unpack

from PIL import Image, ImageOps

ESC = b'\x1b'
GS  = b'\x1d'

# Physical width of the M220 print head/sensor assembly, in dots at
# 203dpi (8 dots/mm). This is not just an empirical estimate: it
# matches the official Windows driver's own device config exactly
# (Labelife's PrinterDetails entry for "M220A" has productDpi=203,
# printWidth="72", maxPrintingWidth="72", printCenter=1 - "printCenter"
# being the vendor's own flag for "center content on the head").
# Override with PHOMEMO_M220_HEAD_WIDTH_DOTS if your unit differs.
PRINTHEAD_WIDTH_DOTS = int(os.environ.get('PHOMEMO_M220_HEAD_WIDTH_DOTS', 576))

CupsRas3 = namedtuple(
    # Documentation at https://www.cups.org/doc/spec-raster.html
    'CupsRas3',
    'MediaClass MediaColor MediaType OutputType AdvanceDistance AdvanceMedia Collate CutMedia Duplex HWResolutionH '
    'HWResolutionV ImagingBoundingBoxL ImagingBoundingBoxB ImagingBoundingBoxR ImagingBoundingBoxT '
    'InsertSheet Jog LeadingEdge MarginsL MarginsB ManualFeed MediaPosition MediaWeight MirrorPrint '
    'NegativePrint NumCopies Orientation OutputFaceUp PageSizeW PageSizeH Separations TraySwitch Tumble cupsWidth '
    'cupsHeight cupsMediaType cupsBitsPerColor cupsBitsPerPixel cupsBitsPerLine cupsColorOrder cupsColorSpace '
    'cupsCompression cupsRowCount cupsRowFeed cupsRowStep cupsNumColors cupsBorderlessScalingFactor cupsPageSizeW '
    'cupsPageSizeH cupsImagingBBoxL cupsImagingBBoxB cupsImagingBBoxR cupsImagingBBoxT cupsInteger1 cupsInteger2 '
    'cupsInteger3 cupsInteger4 cupsInteger5 cupsInteger6 cupsInteger7 cupsInteger8 cupsInteger9 cupsInteger10 '
    'cupsInteger11 cupsInteger12 cupsInteger13 cupsInteger14 cupsInteger15 cupsInteger16 cupsReal1 cupsReal2 '
    'cupsReal3 cupsReal4 cupsReal5 cupsReal6 cupsReal7 cupsReal8 cupsReal9 cupsReal10 cupsReal11 cupsReal12 '
    'cupsReal13 cupsReal14 cupsReal15 cupsReal16 cupsString1 cupsString2 cupsString3 cupsString4 cupsString5 '
    'cupsString6 cupsString7 cupsString8 cupsString9 cupsString10 cupsString11 cupsString12 cupsString13 cupsString14 '
    'cupsString15 cupsString16 cupsMarkerType cupsRenderingIntent cupsPageSizeName'
)

def read_ras3(rdata):
    if not rdata:
        raise ValueError('No data received')

    # Check for magic word (either big-endian or little-endian)
    magic = unpack('@4s', rdata[0:4])[0]
    if magic != b'RaS3' and magic != b'3SaR':
        raise ValueError("This is not in RaS3 format")
    rdata = rdata[4:]  # Strip magic word
    pages = []

    while rdata:  # Loop over all pages
        struct_data = unpack(
            '@64s 64s 64s 64s I I I I I II IIII I I I II I I I I I I I I II I I I I I I I I I I I I I '
            'I I I f ff ffff IIIIIIIIIIIIIIII ffffffffffffffff 64s 64s 64s 64s 64s 64s 64s 64s 64s 64s '
            '64s 64s 64s 64s 64s 64s 64s 64s 64s',
            rdata[0:1796]
        )
        data = [
            # Strip trailing null-bytes of strings
            b.decode().rstrip('\x00') if isinstance(b, bytes) else b
            for b in struct_data
        ]
        header = CupsRas3._make(data)

        # Read image data of this page into a bytearray
        imgdata = rdata[1796:1796 + (header.cupsWidth * header.cupsHeight * header.cupsBitsPerPixel // 8)]
        pages.append((header, imgdata))

        # Remove this page from the data stream, continue with the next page
        rdata = rdata[1796 + (header.cupsWidth * header.cupsHeight * header.cupsBitsPerPixel // 8):]

    return pages

# Vendor-recommended print density (1=lightest .. 15=darkest), used
# when the CUPS "Darkness" option is unset/out of range. Taken from
# the Windows driver's own device config (Labelife
# AimoDetails/PrinterDetails): the M220A entry's density list flags
# 6 as "recommend":"1". The filter previously hard-coded 10 here
# regardless of any argument passed in, which is noticeably darker/
# hotter than the factory recommendation and was reported to over-
# print ("Druckdichte zu hoch").
DEFAULT_DENSITY = 6
MIN_DENSITY = 1
MAX_DENSITY = 15

# select_speed() (ESC N 0D <1-5>) is defined but deliberately never
# called from print_header() below. rastertopm110.py (and this filter,
# until this fix) always forced speed=5 on every M220 job. The vendor
# Windows driver never sends this command at all for M220 - confirmed
# across four separate USB captures of real M220 print jobs, none of
# which contain an ESC N 0D byte sequence - so the printer is left on
# whatever speed its firmware already has set.
#
# This was found while investigating a label printing lighter on
# Windows than on Linux for the same job: the Linux capture had a
# *lower* density byte than the Windows one (6 vs 7) yet printed
# *darker*, which density alone cannot explain. The only other
# difference was this forced speed=5 override (plus a still-unexplained
# extra `1F 11 35 00` command Windows sends and this filter does not).
# Removing the override makes the firmware's own default speed apply,
# matching Windows' behaviour.
def select_speed(file, speed = 5):
    file.write(ESC + b'\x4e' + b'\x0d') # select Print Speed
    file.write(speed.to_bytes(1, 'little'))
    return

def select_density(file, density = DEFAULT_DENSITY):
    file.write(ESC + b'\x4e' + b'\x04') # select Print Density
    file.write(density.to_bytes(1, 'little'))
    return

def select_media_type(file, media_type):
    file.write(b'\x1f' + b'\x11') # select Media Type, 
    file.write(media_type.to_bytes(1, 'little'))
    return

def select_raw_compression(file):
    # US 11 35 00 - sent by the Windows driver right after media type on
    # every M220 job seen so far, but never by rastertopm110/rastertopm220
    # until now. The same byte sequence is used, in the same "right after
    # media type" position, by two unrelated Phomemo families already in
    # this repo - rastertopm04.py and rastertopm832.py - both documenting
    # it as "compression: raw", i.e. telling the firmware the GS v 0 bit
    # image that follows is uncompressed. That matches what this filter
    # already sends (print_raster() never compresses), so emitting it is
    # a safe no-op if the M04/M832 meaning holds for the M220 too.
    #
    # Unconfirmed on real M220 hardware whether it has any visible effect
    # (e.g. on darkness) or is purely a format declaration; added to make
    # the command stream match the vendor driver byte-for-byte (besides
    # the density value itself) for testing. If a real test print shows
    # no difference from omitting it, it can be dropped again.
    file.write(b'\x1f' + b'\x11' + b'\x35' + b'\x00')
    return

def print_header(file, media_type = 10, density = DEFAULT_DENSITY):
    select_density(file, density)
    select_media_type(file, media_type)
    select_raw_compression(file)
    return

def center_on_printhead(image, head_width_dots = PRINTHEAD_WIDTH_DOTS):
    """Return a copy of `image` padded left/right to be exactly
    `head_width_dots` wide, with the original content centred.

    If the page is already as wide as (or wider than) the head, the
    image is returned unchanged - we never crop user content.

    NOTE: by the time this runs, `image` has already gone through
    ImageOps.invert(), so in this bitmap 0 = blank paper/no dot and
    1 = ink/fire the head - the opposite of PIL's usual black=0/
    white=1 reading for mode '1'. The padding must therefore be
    filled with 0, not 1, or the margins print solid black."""
    if image.width >= head_width_dots:
        return image

    offset = (head_width_dots - image.width) // 2

    canvas = Image.new('1', (head_width_dots, image.height), 0)  # 0 = no ink
    canvas.paste(image, (offset, 0))
    return canvas

def print_raster(file, image, line, lines = 0xff, mode = 0):
    file.write(GS + b'v0')   # GS v 0 : print raster bit image
    # 0: normal, 1 double width, 2 double heigh, 3 quadruple
    file.write(mode.to_bytes(1, 'little'))
    # number of bytes / line
    file.write(int((image.width + 7) / 8).to_bytes(2, 'little'))
    # nulber of lines in the image
    file.write(lines.to_bytes(2, 'little'))
    # bit image
    block = image.crop((0, line, image.width, line + lines))
    file.write(block.tobytes())
    return

def print_footer(file):
    file.write(b'\x1f' + b'\xf0' + b'\x05' + b'\x00')
    file.write(b'\x1f' + b'\xf0' + b'\x03' + b'\x00')

pages = read_ras3(sys.stdin.buffer.read())

for i, datatuple in enumerate(pages):
    (header, imgdata) = datatuple

    if header.cupsNumColors != 1:
        raise ValueError('Invalid color space, only monocolor supported')

    im = Image.frombuffer(mode='L', data=imgdata,
                          size=(header.cupsWidth, header.cupsHeight))
    im = ImageOps.invert(im)
    im = im.convert('1')

    # Pad the page out to the full print head width so narrow labels
    # end up centred under the head instead of flush against its left
    # edge. See the module docstring for why this is necessary.
    im = center_on_printhead(im)

    # PPD cupsInteger0 (zero-based) arrives here as cupsInteger1. Fall
    # back to the vendor-recommended density if the option was never
    # set (e.g. 0 from a raw/driverless job) or is out of the
    # printer's valid 1-15 range.
    density = header.cupsInteger1
    if not (MIN_DENSITY <= density <= MAX_DENSITY):
        density = DEFAULT_DENSITY

    line = 0

    with os.fdopen(sys.stdout.fileno(), "wb", closefd=False) as stdout:
        print_header(stdout, header.cupsMediaType, density)
        lines = im.height
        print_raster(stdout, im, line, lines)
        print_footer(stdout)
