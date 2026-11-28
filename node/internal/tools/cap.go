package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"sync"
	"time"

	"topcoat/node/internal/providers/capability"
)

// DockerAPI is the read-only view of the Docker daemon the capabilities use (see Docker).
type DockerAPI interface {
	Get(ctx context.Context, path string, q url.Values) ([]byte, bool, error)
}

// Options are what the capabilities need from the machine. The zero value of every field is a
// sensible default, so a test only sets what it fakes.
type Options struct {
	// Docker reads the Docker API; nil = docker.* and compose.* answer "not configured".
	Docker DockerAPI
	// DockerWrite restarts containers through a second socket proxy (E3-01b); nil = docker.restart
	// is not offered. RestartContainers and RestartUnits are glob lists of what may be restarted;
	// empty means nothing (default deny) and the capability is not offered.
	DockerWrite       DockerWriteAPI
	RestartContainers []string
	RestartUnits      []string
	// Commander runs journalctl and systemctl; nil = ExecCommander.
	Commander Commander
	// ProcRoot and EtcRoot are where /proc and /etc are read (tests point them at a temp dir).
	ProcRoot, EtcRoot string
	// HostProc is the host's /proc mounted into a container node (TOPCOAT_HOST_PROC): system.memory
	// reads the processes there. Empty = ProcRoot.
	HostProc string
	// ComposeRoots are the only directories compose and .env files are read from or listed. Empty
	// means none: the default is deny.
	ComposeRoots []string
	// DocsRoots are the only directories the knowledge base reads Markdown and text files from
	// (docs.list, docs.read); empty means none: the capabilities are not offered at all.
	// DocsExclude adds globs to the exclude list that cannot be switched off (DefaultDocsExclude).
	DocsRoots, DocsExclude []string
	// ReadRoots are the only directories files.list and files.read look into (HOST-01); empty means
	// none: the capabilities are not offered at all.
	ReadRoots []string
	// winRules applies the Windows path rules; set by NewRegistry on Windows and by tests.
	winRules bool
	// Shell runs shell.read through the readshell socket unit (an unprivileged OS user); nil =
	// shell.read is not offered (default off).
	Shell ShellAPI
	// Windows runs the fixed PowerShell scripts of the windows.* capabilities; nil = they are not
	// offered (set on Windows nodes; they run only where config.capabilities lists them).
	Windows PowerShellAPI
	// Web switches the web capabilities on (web.search needs the SearXNG URL, web.fetch an explicit
	// opt-in): they are not offered otherwise.
	Web WebOptions
	// Checks are the node-configured targets of net.check and certs.expiry (E3-04).
	Checks CheckOptions
	// Connectors holds the credentials of the systems the connector capabilities ask (E2-04c);
	// nil = none are configured and their capabilities are not offered.
	Connectors *Connectors
	omada      *omadaClient // built by NewRegistry from Connectors
	keycloak   *keycloakClient
	// StatFS reads the usage of a mount point (tests; the default is statfs(2)).
	StatFS func(path string) (FSStat, error)
	// Now is the clock (tests).
	Now func() time.Time
}

func (o Options) now() time.Time {
	if o.Now != nil {
		return o.Now()
	}
	return time.Now()
}

// fn is a capability made of a function.
type fn struct {
	name, desc, schema string
	run                func(ctx context.Context, in json.RawMessage) (any, error)
}

func (f fn) Name() string                 { return f.name }
func (f fn) InputSchema() json.RawMessage { return json.RawMessage(f.schema) }
func (f fn) Info() capability.Info        { return capability.Info{Description: f.desc, ReadOnly: true} }

func (f fn) Invoke(ctx context.Context, in json.RawMessage) (json.RawMessage, error) {
	v, err := f.run(ctx, in)
	if err != nil {
		return nil, err
	}
	return marshalOutput(v)
}

// marshalOutput is json.Marshal without HTML escaping: the reader is the hub's JSON decoder, never
// a browser, and escaping every <, > and & as a unicode escape made a page read as markup a third larger
// (an Ultimate Guitar page of 211 kB came to 288 kB and passed the output limit).
func marshalOutput(v any) (json.RawMessage, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	// Encode ends with a newline; the output is the value alone
	return json.RawMessage(bytes.TrimSuffix(buf.Bytes(), []byte{'\n'})), nil
}

var errNoDocker = errors.New("no Docker API is configured on this node (config.tools.docker_host)")

// NewRegistry returns the registry with every capability of E2-04. All of them are read-only.
func NewRegistry(o Options) *capability.Registry {
	r := capability.NewRegistry()
	c := o.Commander
	if c == nil {
		c = ExecCommander{}
	}
	o.Commander = c
	if o.Web.Seen == nil { // one memory for all web capabilities of this node
		o.Web.Seen = NewURLMemory()
	}
	if o.Connectors != nil && o.Connectors.Omada != nil {
		o.omada = newOmadaClient(o.Connectors.Omada)
	}
	if o.Connectors != nil && o.Connectors.Keycloak != nil {
		o.keycloak = newKeycloakClient(o.Connectors.Keycloak)
	}
	for _, cp := range []capability.Capability{
		systemInfo(o), systemMemory(o), diskUsage(o),
		dockerPS(o), dockerInspect(o), dockerLogs(o),
		composeProjectsCap(o), composeFileCap(o), composeLogs(o),
		journalLogs(o), serviceStatus(o), serviceFailed(o),
		// collecting capabilities: one call instead of six
		healthCheck(o), logsSearch(o),
		// what runs here, for the hub's inventory
		inventorySnapshot(o),
	} {
		if err := r.Register(cp); err != nil {
			panic(err) // a duplicate name is a programming error caught by the tests
		}
	}
	// The knowledge base's file access exists only where directories are released (default deny)
	if len(o.DocsRoots) > 0 {
		mustRegister(r, docsListCap(o))
		mustRegister(r, docsReadCap(o))
	}
	// Reading files of the host exists only where a person released folders (default deny)
	if len(o.ReadRoots) > 0 {
		mustRegister(r, filesListCap(o))
		mustRegister(r, filesReadCap(o))
	}
	if o.Web.SearxngURL != "" {
		mustRegister(r, webSearch(o))
		mustRegister(r, webImages(o))
	}
	if o.Web.Fetch {
		mustRegister(r, webFetch(o))
		mustRegister(r, webImage(o))
	}
	// the state-changing capabilities (E3-01b): only where the config names what may be restarted
	if o.DockerWrite != nil && len(o.RestartContainers) > 0 {
		mustRegister(r, dockerRestart(o))
		mustRegister(r, composeRestart(o))
	}
	if len(o.RestartUnits) > 0 {
		mustRegister(r, serviceRestart(o))
	}
	// the read-only shell exists only where the maintainer released it (default off)
	if o.Shell != nil {
		mustRegister(r, shellRead(o))
	}
	// the Windows read capabilities exist only on Windows nodes
	if o.Windows != nil {
		for _, cp := range windowsCaps(o) {
			mustRegister(r, cp)
		}
	}
	// the collecting capabilities that look at configured targets exist only with targets: the
	// caller never names a host
	if len(o.Checks.CertTargets) > 0 {
		mustRegister(r, certsExpiry(o))
	}
	if len(o.Checks.NetTargets) > 0 || len(o.Checks.DNSNames) > 0 {
		mustRegister(r, netCheck(o))
	}
	// Connector capabilities exist only where the connectors file has the section: a node that
	// cannot reach Uptime Kuma must not offer the agent a tool that can only fail.
	if o.Connectors != nil && o.Connectors.Kuma != nil {
		mustRegister(r, kumaMonitors(o))
	}
	if o.Connectors != nil && o.Connectors.Omada != nil {
		for _, cp := range omadaCaps(o) {
			mustRegister(r, cp)
		}
	}
	if o.Connectors != nil && o.Connectors.Keycloak != nil {
		for _, cp := range keycloakCaps(o) {
			mustRegister(r, cp)
		}
	}
	return r
}

func mustRegister(r *capability.Registry, cp capability.Capability) {
	if err := r.Register(cp); err != nil {
		panic(err)
	}
}

// parallel runs fn for 0..n-1 with at most width in flight.
func parallel(n, width int, fn func(i int)) {
	sem := make(chan struct{}, width)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		sem <- struct{}{}
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			defer func() { <-sem }()
			fn(i)
		}(i)
	}
	wg.Wait()
}
