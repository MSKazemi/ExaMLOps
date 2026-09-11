import { describe, expect, it } from 'vitest'
import {
  CONSOLE_FOR_PREFIX,
  cliFilterHref,
  cliPrefixesFor,
  commandLine,
  consoleFor,
  defaultValues,
  effectiveTier,
  filterByPrefixes,
  groupByPanel,
  missingRequired,
  outputShape,
  searchCommands,
  toArgs,
  fromArgs,
  type CliCatalog,
  type CliCommand,
} from './cli'
import { HOME_ITEM, NAV_SECTIONS, UTILITY_NAV } from './nav'

const cmd = (over: Partial<CliCommand>): CliCommand => ({
  path: 'x',
  group: 'x',
  panel: 'Getting Started',
  help: '',
  short_help: '',
  examples: [],
  tier: 'read',
  reason: null,
  params: [],
  forced_args: [],
  ...over,
})

const DRIFT = cmd({
  path: 'drift status',
  group: 'drift',
  panel: 'Monitoring & Quality',
  short_help: 'Show prediction drift status',
  params: [
    { name: 'model', kind: 'argument', type: 'string' },
    { name: 'watch', kind: 'option', type: 'bool', flag: true, opts: ['--watch'], blocked: true },
  ],
})
const COST = cmd({
  path: 'models cost',
  group: 'models',
  panel: 'Models & Registry',
  params: [
    { name: 'model', kind: 'argument', type: 'string', required: true },
    { name: 'record', kind: 'option', type: 'bool', flag: true, opts: ['--record'], persisting: true },
  ],
})
const CARD = cmd({
  path: 'cards model',
  group: 'cards',
  params: [
    { name: 'model', kind: 'argument', type: 'string', required: true },
    { name: 'save', kind: 'option', type: 'bool', flag: true, opts: ['--save'], secondary_opts: ['--no-save'], default: true, persisting: true },
    { name: 'out', kind: 'option', type: 'string', opts: ['--out'], path: true },
  ],
})
const SECRET = cmd({
  path: 'secrets set',
  group: 'secrets',
  tier: 'admin',
  params: [
    { name: 'path', kind: 'argument', type: 'string', required: true },
    { name: 'value', kind: 'argument', type: 'string', required: true, secret: true },
  ],
})
const ASK = cmd({
  path: 'ask',
  group: 'ask',
  params: [{ name: 'question', kind: 'argument', type: 'string', nargs: -1 }],
})

const CATALOG: CliCatalog = {
  commands: [DRIFT, COST, CARD, SECRET, ASK],
  total: 5,
  tiers: { read: 4, admin: 1, destructive: 0, cli_only: 0 },
  unclassified: [],
  panels: ['Getting Started', 'Models & Registry', 'Monitoring & Quality'],
}

describe('catalog helpers', () => {
  it('groups by the CLI panel order', () => {
    const panels = groupByPanel(CATALOG).map(([p]) => p)
    expect(panels.indexOf('Models & Registry')).toBeLessThan(panels.indexOf('Monitoring & Quality'))
  })

  it('ranks a path match above a help-text match', () => {
    expect(searchCommands(CATALOG.commands, 'drift')[0].path).toBe('drift status')
    expect(searchCommands(CATALOG.commands, '')).toHaveLength(5)
  })

  it('filters by console prefixes', () => {
    expect(filterByPrefixes(CATALOG.commands, ['models']).map((c) => c.path)).toEqual(['models cost'])
    expect(filterByPrefixes(CATALOG.commands, [])).toHaveLength(5)
  })
})

describe('form → args', () => {
  it('sends only what differs from the defaults, and never a blocked flag', () => {
    const values = { ...defaultValues(DRIFT), model: 'JPCP', watch: true }
    expect(toArgs(DRIFT, values)).toEqual({ model: 'JPCP' })
  })

  it('turns a default-on flag off explicitly', () => {
    const values = { ...defaultValues(CARD), model: 'jpcp', save: false }
    expect(toArgs(CARD, values)).toEqual({ model: 'jpcp', save: false })
  })

  it('splits a variadic argument on whitespace', () => {
    expect(toArgs(ASK, { question: 'how many models' })).toEqual({ question: ['how', 'many', 'models'] })
  })

  it('reports missing required params', () => {
    expect(missingRequired(COST, {})).toEqual(['model'])
    expect(missingRequired(COST, { model: 'jpcp' })).toEqual([])
  })
})

describe('effective tier (mirrors the server)', () => {
  it('a persisting flag raises a read to admin', () => {
    expect(effectiveTier(COST, { model: 'jpcp' })).toBe('read')
    expect(effectiveTier(COST, { model: 'jpcp', record: true })).toBe('admin')
  })

  it('a persisting flag that defaults on counts when left alone', () => {
    expect(effectiveTier(CARD, { model: 'jpcp' })).toBe('admin')
    expect(effectiveTier(CARD, { model: 'jpcp', save: false })).toBe('read')
  })

  it('a filesystem path raises a read to admin', () => {
    expect(effectiveTier(CARD, { model: 'jpcp', save: false, out: 'card.md' })).toBe('admin')
  })
})

describe('command line preview', () => {
  it('renders options, flags and positionals like the terminal', () => {
    expect(commandLine(COST, { model: 'jpcp', record: true }, 'json')).toBe('exa --json models cost --record jpcp')
    expect(commandLine(CARD, { model: 'jpcp', save: false }, 'text')).toBe('exa cards model --no-save jpcp')
  })

  it('masks secret values and quotes what the shell would split', () => {
    expect(commandLine(SECRET, { path: 'cp/token', value: 'hunter2' }, 'text')).toBe("exa secrets set cp/token '***'")
    expect(commandLine(ASK, { question: ['a b'] }, 'text')).toBe("exa ask 'a b'")
  })

  it('keeps a -- separator only when a positional starts with a dash', () => {
    expect(commandLine(DRIFT, { model: '-x' }, 'text')).toBe('exa drift status -- -x')
  })
})

describe("a command's own --yes", () => {
  const DEL = cmd({
    path: 'project delete',
    group: 'project',
    tier: 'destructive',
    params: [
      { name: 'name', kind: 'argument', type: 'string', required: true },
      { name: 'yes', kind: 'option', type: 'bool', flag: true, opts: ['--yes', '-y'], implied: true },
    ],
  })

  it('is never sent from the form but shows in the equivalent command', () => {
    const args = toArgs(DEL, { name: 'old', yes: false })
    expect(args).toEqual({ name: 'old' })
    expect(commandLine(DEL, args, 'text')).toBe('exa project delete --yes old')
  })
})

describe('output shape', () => {
  it('picks a renderer from the parsed JSON', () => {
    expect(outputShape([{ a: 1 }], '')).toBe('table')
    expect(outputShape({ a: 1 }, '')).toBe('record')
    expect(outputShape([], '')).toBe('empty')
    expect(outputShape(null, 'hello')).toBe('text')
    expect(outputShape([1, 2], '')).toBe('json')
  })
})

describe('console ⇄ CLI links', () => {
  const routes = new Set(
    [HOME_ITEM, ...NAV_SECTIONS.flatMap((s) => s.items), ...UTILITY_NAV].map((i) => i.path).concat(['/serve/challenger']),
  )

  it('every console a CLI prefix points at is a real route', () => {
    const dead = Object.entries(CONSOLE_FOR_PREFIX).filter(([, r]) => !routes.has(r))
    expect(dead).toEqual([])
  })

  it('resolves the most specific prefix', () => {
    expect(consoleFor('serve ab start')).toBe('/serve/traffic')
    expect(consoleFor('serve autoscale set')).toBe('/serve/scaling')
    expect(consoleFor('serve reload')).toBeNull()
    expect(consoleFor('drift input baseline')).toBe('/operate/drift')
  })

  it('maps a console back to its CLI prefixes', () => {
    expect(cliPrefixesFor('/operate/drift')).toEqual(['drift'])
    expect(cliPrefixesFor('/platform/projects/research')).toEqual(['project', 'namespace', 'connection', 'workbench'])
    expect(cliPrefixesFor('/')).toEqual([])
    expect(cliFilterHref(['drift'])).toBe('/platform/cli?filter=drift')
  })
})

describe('fromArgs — "Run again" restores a previous run\x27s form', () => {
  const RICH = cmd({
    path: 'eval run',
    params: [
      { name: 'model', kind: 'argument', type: 'string', required: true },
      { name: 'limit', kind: 'option', type: 'int', opts: ['--limit'], default: 50 },
      { name: 'tag', kind: 'option', type: 'string', opts: ['--tag'], multiple: true },
      { name: 'dry_run', kind: 'option', type: 'bool', flag: true, opts: ['--dry-run'] },
      { name: 'save', kind: 'option', type: 'bool', flag: true, opts: ['--save'], secondary_opts: ['--no-save'], default: true },
      { name: 'token', kind: 'option', type: 'string', opts: ['--token'], secret: true, required: true },
      { name: 'watch', kind: 'option', type: 'bool', flag: true, opts: ['--watch'], blocked: true },
    ],
  })

  it('round-trips the args the form sent (scalars, lists, both flag polarities)', () => {
    const values = { model: 'JPCP', limit: '10', tag: 'a\nb', dry_run: true, save: false }
    const back = fromArgs(RICH, toArgs(RICH, values))
    expect(toArgs(RICH, { ...defaultValues(RICH), ...back })).toEqual(toArgs(RICH, values))
    expect(back).toMatchObject({ model: 'JPCP', limit: '10', tag: 'a\nb', dry_run: true, save: false })
  })

  it('never restores a secret — the stored value is the mask, so it must be typed again', () => {
    const back = fromArgs(RICH, { model: 'JPCP', token: '***' })
    expect(back).not.toHaveProperty('token')
    expect(missingRequired(RICH, { ...defaultValues(RICH), ...back })).toEqual(['token'])
  })

  it('restores a variadic argument the way the form asks for it — space-separated', () => {
    expect(fromArgs(ASK, toArgs(ASK, { question: 'how many models' }))).toEqual({ question: 'how many models' })
  })

  it('ignores blocked params and params the command no longer has', () => {
    const back = fromArgs(RICH, { model: 'JPCP', watch: true, removed_since: 'x' })
    expect(back).toEqual({ model: 'JPCP' })
  })
})
