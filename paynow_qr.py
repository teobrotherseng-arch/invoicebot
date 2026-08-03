"""
Generates a PayNow SGQR payload string and renders it as a QR code image.

This follows the public EMVCo Merchant-Presented QR spec with the PayNow
merchant account template, which is the same standard used by Singapore
banking apps to build PayNow QR codes.

Payload is a fixed amount, non-editable, single-use QR tied to an invoice
number (put in the "Additional Data - Bill Number" field).
"""
import io
import qrcode


def _tlv(tag: str, value: str) -> str:
    """Tag-Length-Value encoding used throughout the EMV QR spec."""
    length = f"{len(value):02d}"
    return f"{tag}{length}{value}"


def _crc16_ccitt(payload: str) -> str:
    """CRC-16/CCITT-FALSE over the payload, as required by the EMV QR spec."""
    crc = 0xFFFF
    for b in payload.encode("utf-8"):
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def build_paynow_payload(
    amount: float, invoice_number: str, proxy_type: str, proxy_value: str,
    merchant_name: str = "NA", expiry_yyyymmdd: str = None
) -> str:
    """
    amount: fixed amount to pay
    invoice_number: put into the bill-reference field so it shows on the payer's bank app
    proxy_type: "UEN" or "MOBILE"
    proxy_value: the company's UEN or PayNow-linked mobile number (e.g. "+6591234567")
    expiry_yyyymmdd: optional QR expiry date, e.g. "20261231"
    """
    proxy_type_code = "2" if proxy_type.upper() == "UEN" else "0"  # 0=mobile, 2=UEN

    merchant_account_fields = (
        _tlv("00", "SG.PAYNOW")
        + _tlv("01", proxy_type_code)
        + _tlv("02", proxy_value)
        + _tlv("03", "0")  # 0 = amount is fixed/non-editable by payer
    )
    if expiry_yyyymmdd:
        merchant_account_fields += _tlv("04", expiry_yyyymmdd)

    additional_data = _tlv("01", invoice_number[:25])  # bill number / reference

    fields = "".join([
        _tlv("00", "01"),                       # Payload Format Indicator
        _tlv("01", "12"),                       # Point of Initiation: 12 = dynamic (fixed amount)
        _tlv("26", merchant_account_fields),    # PayNow merchant account info
        _tlv("52", "0000"),                     # Merchant Category Code (unspecified)
        _tlv("53", "702"),                      # Currency: 702 = SGD
        _tlv("54", f"{amount:.2f}"),            # Transaction amount
        _tlv("58", "SG"),                       # Country code
        _tlv("59", merchant_name[:25] or "NA"), # Merchant name
        _tlv("60", "Singapore"),                # Merchant city
        _tlv("62", additional_data),            # Additional data (bill number)
    ])

    payload_without_crc = fields + "6304"
    crc = _crc16_ccitt(payload_without_crc)
    return payload_without_crc + crc


def generate_paynow_qr_image(
    amount: float, invoice_number: str, proxy_type: str, proxy_value: str,
    merchant_name: str = "NA", expiry_yyyymmdd: str = None
) -> bytes:
    """Returns PNG image bytes of the PayNow QR code for the given amount."""
    payload = build_paynow_payload(amount, invoice_number, proxy_type, proxy_value, merchant_name, expiry_yyyymmdd)
    img = qrcode.make(payload, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
