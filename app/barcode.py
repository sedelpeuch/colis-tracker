from __future__ import annotations

import io

import barcode
from barcode.writer import ImageWriter


def generate_barcode_png(tracking_code: str) -> bytes:
    writer = ImageWriter()  # ty: ignore[call-non-callable]
    writer.set_options({"write_text": False, "quiet_zone": 2})
    code = barcode.Code128(tracking_code, writer=writer)
    buffer = io.BytesIO()
    code.write(buffer)
    return buffer.getvalue()
