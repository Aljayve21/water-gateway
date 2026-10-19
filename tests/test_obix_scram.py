"""SCRAM login tests for the oBIX client.

The handshake is checked against the RFC 7677 SCRAM-SHA-256 test vector so a
regression in the proof or server-signature maths fails loudly without needing
a Niagara station.
"""

from __future__ import annotations

import base64
from unittest import mock

import pytest

from gateway.obix import ObixClient, ObixError

CLIENT_NONCE = "rOprNGfwEbeRWgbNEkqO"
SERVER_FIRST = (
    "r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
    "s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096"
)
SERVER_FINAL = "v=6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
EXPECTED_CLIENT_FIRST = "n,,n=user,r=" + CLIENT_NONCE
EXPECTED_CLIENT_FINAL = (
    "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
    "p=dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
)


def _client() -> ObixClient:
    return ObixClient(
        "https://example.test/obix",
        username="user",
        password="pencil",
        auth="scram",
    )


def test_scram_sha256_matches_rfc7677() -> None:
    client = _client()
    sent: list[str] = []

    def fake_post(url: str, body: str) -> str:
        sent.append(body)
        return SERVER_FIRST if "sendClientFirstMessage" in body else SERVER_FINAL

    with (
        mock.patch("gateway.obix.os.urandom", return_value=base64.b64decode(CLIENT_NONCE)),
        mock.patch.object(client, "_post_login_support", side_effect=fake_post),
    ):
        client._login()

    assert sent[0].split("clientFirstMessage=", 1)[1] == EXPECTED_CLIENT_FIRST
    assert sent[1].split("clientFinalMessage=", 1)[1] == EXPECTED_CLIENT_FINAL
    assert client._logged_in


def test_scram_rejects_bad_server_signature() -> None:
    client = _client()

    def fake_post(url: str, body: str) -> str:
        return SERVER_FIRST if "sendClientFirstMessage" in body else "v=deadbeef"

    with (
        mock.patch("gateway.obix.os.urandom", return_value=base64.b64decode(CLIENT_NONCE)),
        mock.patch.object(client, "_post_login_support", side_effect=fake_post),
    ):
        with pytest.raises(ObixError, match="server signature"):
            client._login()


def test_scram_nonce_must_extend_client_nonce() -> None:
    client = _client()

    def fake_post(url: str, body: str) -> str:
        return "r=somethingelse,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096"

    with (
        mock.patch("gateway.obix.os.urandom", return_value=base64.b64decode(CLIENT_NONCE)),
        mock.patch.object(client, "_post_login_support", side_effect=fake_post),
    ):
        with pytest.raises(ObixError, match="nonce"):
            client._login()
