"""Minimal, stateful telnet protocol filter.

MUD servers use almost none of the telnet protocol; we refuse every option
negotiation, strip subnegotiations, and pass application bytes through
untouched. The filter is stateful because IAC sequences and multi-byte
EUC-KR characters can both be split across TCP packets.
"""

from dataclasses import dataclass

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
GA = 249  # "go ahead" — some servers mark the end of a prompt with this
SE = 240
EOR = 239

_NEGOTIATION = (DO, DONT, WILL, WONT)


@dataclass
class TelnetEvent:
    data: bytes       # application bytes with all telnet sequences removed
    replies: bytes    # negotiation refusals to send back to the server
    prompt_marks: int # number of GA/EOR marks seen in this chunk


class TelnetFilter:
    def __init__(self) -> None:
        self._state = "data"  # data | iac | opt | sb | sb_iac
        self._pending_cmd = 0

    def feed(self, chunk: bytes) -> TelnetEvent:
        data = bytearray()
        replies = bytearray()
        marks = 0

        for b in chunk:
            if self._state == "data":
                if b == IAC:
                    self._state = "iac"
                else:
                    data.append(b)
            elif self._state == "iac":
                if b == IAC:  # escaped literal 0xFF (valid inside EUC-KR range)
                    data.append(IAC)
                    self._state = "data"
                elif b in _NEGOTIATION:
                    self._pending_cmd = b
                    self._state = "opt"
                elif b == SB:
                    self._state = "sb"
                else:
                    if b in (GA, EOR):
                        marks += 1
                    self._state = "data"
            elif self._state == "opt":
                # Refuse everything: DO x -> WONT x, WILL x -> DONT x.
                if self._pending_cmd == DO:
                    replies += bytes((IAC, WONT, b))
                elif self._pending_cmd == WILL:
                    replies += bytes((IAC, DONT, b))
                self._state = "data"
            elif self._state == "sb":
                if b == IAC:
                    self._state = "sb_iac"
            elif self._state == "sb_iac":
                if b == SE:
                    self._state = "data"
                else:  # IAC IAC (escaped byte) or stray command: stay in SB
                    self._state = "sb"

        return TelnetEvent(bytes(data), bytes(replies), marks)
