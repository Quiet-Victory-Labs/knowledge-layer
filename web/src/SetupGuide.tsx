import { useEffect, useRef, useState } from 'react';
import { Button, Callout, Flex, Heading, Select, Tabs, Text, TextArea } from './ui';
import { getInstallationSetup, type InstallationSetup } from './platformApi';
import './setup.css';

type SetupGuideProps = {
  sourceCount: number;
  onConnect: () => void;
  onBuild: () => void;
  onDone: () => void;
};

type Step = 'agent' | 'sources' | 'build';
type Client = keyof InstallationSetup['agent']['clients'];

const CLIENT_CONFIG_PATH: Record<Client, string> = {
  codex: '.codex/config.toml',
  claude: '.mcp.json',
  cursor: '.cursor/mcp.json',
};

const VERIFY_PROMPT = 'Use my configured knowledge-layer MCP to call get_build_handoff. Tell me which sources are visible before making changes. Once I connect sources and start a build, use build_knowledge to claim bounded work, inspect both meaning and contract tracks, draft supported knowledge, and preserve uncertainties. Stop for human review and acceptance.';

export default function SetupGuide({ sourceCount, onConnect, onBuild, onDone }: SetupGuideProps) {
  const [setup, setSetup] = useState<InstallationSetup | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [step, setStep] = useState<Step>('agent');
  const [client, setClient] = useState<Client>('codex');
  const [copied, setCopied] = useState('');
  const [fallbackCopy, setFallbackCopy] = useState('');
  const fallbackRef = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    void getInstallationSetup(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setSetup(result);
      setStep(result.next_step);
    }).catch((reason) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Setup could not be loaded.');
    });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    if (fallbackCopy) {
      fallbackRef.current?.focus();
      fallbackRef.current?.select();
    }
  }, [fallbackCopy]);

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      if (mounted.current) { setCopied(label); setFallbackCopy(''); }
    } catch {
      if (mounted.current) { setFallbackCopy(value); setCopied(''); }
    }
  }

  return <section className="setup-guide" aria-labelledby="setup-title">
    <Flex justify="between" align="center" gap="3" wrap="wrap"><Heading as="h1" size="7" id="setup-title">Set up knowledge layer</Heading><Button variant="ghost" color="gray" onClick={onDone}>Close setup</Button></Flex>
    {error && <Callout.Root color="red" role="alert"><Callout.Text>{error}</Callout.Text><Button variant="ghost" color="red" onClick={() => setRevision((current) => current + 1)}>Retry</Button></Callout.Root>}
    {!setup && !error && <Text role="status" color="gray">Loading setup…</Text>}
    {setup && <Tabs.Root value={step} onValueChange={(next) => { setStep(next as Step); setCopied(''); setFallbackCopy(''); }}>
      <Tabs.List><Tabs.Trigger value="agent">1. Agent</Tabs.Trigger><Tabs.Trigger value="sources">2. Sources</Tabs.Trigger><Tabs.Trigger value="build">3. Build</Tabs.Trigger></Tabs.List>
      <Tabs.Content value="agent" className="setup-step">
        <Heading as="h2" size="5">Connect your agent</Heading>
        <Text as="p" size="2" color="gray">Add this installation to the agent you already use.</Text>
        <label className="setup-field"><Text size="2">Agent</Text><Select.Root value={client} onValueChange={(next) => { setClient(next as Client); setCopied(''); }}><Select.Trigger aria-label="Agent client" /><Select.Content><Select.Item value="codex">Codex</Select.Item><Select.Item value="claude">Claude</Select.Item><Select.Item value="cursor">Cursor</Select.Item></Select.Content></Select.Root></label>
        <Text as="p" size="2">Save the configuration in <code>{CLIENT_CONFIG_PATH[client]}</code> at the root of the project where you run {client === 'codex' ? 'Codex' : client === 'claude' ? 'Claude' : 'Cursor'}.</Text>
        <TextArea className="setup-config" readOnly value={setup.agent.clients[client]} aria-label={`${client} MCP configuration`} rows={7} onFocus={(event) => event.currentTarget.select()} />
        {setup.agent.auth === 'bearer' && <Text as="p" size="2" color="gray">Set {setup.agent.token_env} in your agent’s environment using the token from your installation operator.{setup.agent.token_file_hint ? ` The server’s token file is ${setup.agent.token_file_hint}.` : ''} This configuration contains a reference, not the token.</Text>}
        {client === 'codex' && <Text as="p" size="2" color="gray">Trust this project in Codex, then run <code>codex mcp list</code> from the project and confirm <code>knowledge_layer</code> appears. Codex ignores a project’s MCP configuration until the project is trusted.</Text>}
        {client !== 'codex' && <Text as="p" size="2" color="gray">Confirm your agent client’s project and MCP trust prompts, then start a new agent session.</Text>}
        <Flex gap="3" wrap="wrap"><Button onClick={() => void copy(setup.agent.clients[client], 'Configuration copied.')}>Copy configuration</Button><Button variant="soft" color="gray" onClick={() => void copy(VERIFY_PROMPT, 'Agent prompt copied.')}>Copy agent prompt</Button></Flex>
        <Text as="p" size="2" color="gray">Run the prompt in a new agent session. A successful <code>get_build_handoff</code> response with your visible sources verifies the connection.</Text>
        <details className="setup-details"><summary>Endpoint and command</summary><Text as="p" size="2">{setup.agent.endpoint}</Text>{setup.agent.command_template && <><pre>{setup.agent.command_template}</pre><Button size="1" variant="soft" color="gray" onClick={() => void copy(setup.agent.command_template, 'Command copied.')}>Copy command</Button></>}</details>
        <Flex justify="end"><Button variant="soft" onClick={() => setStep('sources')}>Continue to sources</Button></Flex>
      </Tabs.Content>
      <Tabs.Content value="sources" className="setup-step">
        <Heading as="h2" size="5">Connect your data</Heading>
        <Text as="p" size="2" color="gray">Choose a database, warehouse, object store, or files available to this installation.</Text>
        {setup.mode === 'local' && <Text as="p" size="2">Your connected sources are available to your installation’s agent. Only you can accept and publish its drafts.</Text>}
        <Flex gap="3" wrap="wrap"><Button onClick={onConnect}>Connect source</Button>{sourceCount > 0 && <Button variant="soft" color="gray" onClick={() => setStep('build')}>Continue with {sourceCount} {sourceCount === 1 ? 'source' : 'sources'}</Button>}</Flex>
      </Tabs.Content>
      <Tabs.Content value="build" className="setup-step">
        <Heading as="h2" size="5">Build your knowledge</Heading>
        <Text as="p" size="2" color="gray">Start a bounded build, then have your connected agent claim the work. Review its definitions, relationships, and metrics before accepting and publishing.</Text>
        {sourceCount > 0 ? <Button onClick={onBuild} disabled={!setup.builds.available}>Open build</Button> : <Button onClick={onConnect}>Connect your first source</Button>}
        {!setup.builds.available && <Text as="p" size="2" color="orange">The installation operator needs to enable builds.</Text>}
      </Tabs.Content>
      {copied && <Text as="p" size="2" role="status">{copied}</Text>}
      {fallbackCopy && <label className="setup-field"><Text size="2">Copy this text manually</Text><TextArea ref={fallbackRef} readOnly value={fallbackCopy} rows={5} onFocus={(event) => event.currentTarget.select()} /></label>}
      {setup.checks.length > 0 && <details className="setup-details"><summary>Installation checks</summary><ul>{setup.checks.map((check) => <li key={check.id}><Text as="p" size="2" color={check.status === 'action' ? 'orange' : 'gray'}>{check.title}</Text><Text as="p" size="2" color="gray">{check.detail}</Text></li>)}</ul></details>}
    </Tabs.Root>}
  </section>;
}
