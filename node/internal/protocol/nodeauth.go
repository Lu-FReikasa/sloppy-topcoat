package protocol

import (
	"crypto/ed25519"
	"encoding/base64"
)

// Node authentication by signature (ARCHITECTURE D12, docs/PROTOCOL.md "Session").

// AuthScheme is the Authorization scheme of the node WebSocket upgrade.
const AuthScheme = "Topcoat-Ed25519"

// ChallengeRequest is the POST /api/node/challenge body (challenge_request.schema.json).
type ChallengeRequest struct {
	NodeID string `json:"node_id"`
}

// ChallengeResponse is its answer (challenge_response.schema.json).
type ChallengeResponse struct {
	Nonce     string `json:"nonce"`
	ExpiresIn int    `json:"expires_in"`
}

// AuthMessage is the byte string the node signs for one nonce.
func AuthMessage(nodeID, nonce string) []byte {
	return []byte("topcoat-node-auth-v1\n" + nodeID + "\n" + nonce)
}

// AuthorizationHeader signs nonce and returns the Authorization header value.
func AuthorizationHeader(key ed25519.PrivateKey, nodeID, nonce string) string {
	sig := ed25519.Sign(key, AuthMessage(nodeID, nonce))
	return AuthScheme + " " + nodeID + "." + nonce + "." + base64.RawURLEncoding.EncodeToString(sig)
}
