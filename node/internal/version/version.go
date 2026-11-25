// Package version holds the agent and protocol versions.
package version

// Version is the agent version. Override at build time with
//
//	-ldflags "-X topcoat/node/internal/version.Version=1.2.3"
var Version = "0.1.0-dev"

// UpdateKeys are the public keys (comma-separated base64 ed25519, at most two) that sign updates
// of this build (E2-06, D50). A build without keys cannot be updated through the hub. Set at build
// time with
//
//	-ldflags "-X topcoat/node/internal/version.UpdateKeys=<base64>"
var UpdateKeys = ""

// ProtocolVersion is the node <-> hub protocol version this agent speaks (see proto/v1).
// v1 froze the v0 draft (E2-01); hubs accept v0 nodes for one more release.
const ProtocolVersion = 1
