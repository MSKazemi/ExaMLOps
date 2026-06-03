import { useState } from 'react'
import { X, ChevronRight, ChevronLeft, Loader2, CheckCircle2, Terminal } from 'lucide-react'
import { useScaffoldPreview, useScaffoldCreate, type ScaffoldBody, type ScaffoldPreview } from '@/lib/api'

const TASKS = [
  { value: 'performance_prediction', label: 'Performance Prediction' },
  { value: 'power_consumption_prediction', label: 'Power Consumption Prediction' },
  { value: 'anomaly_detection', label: 'Anomaly Detection' },
]

const TASK_TYPES = [
  { value: 'regression', label: 'Regression' },
  { value: 'classification', label: 'Classification' },
]

const DIRECTIONS = [
  { value: 'lower_is_better', label: 'Lower is better (e.g. RMSE)' },
  { value: 'higher_is_better', label: 'Higher is better (e.g. accuracy)' },
]

const DEFAULT_FORM: ScaffoldBody = {
  name: '',
  task: 'performance_prediction',
  task_type: 'regression',
  promotion_metric: 'rmse',
  promotion_threshold: 100.0,
  promotion_direction: 'lower_is_better',
  force: false,
}

interface Props {
  onClose: () => void
}

export function ScaffoldWizard({ onClose }: Props) {
  const [step, setStep] = useState<1 | 2 | 3>(1)
  const [form, setForm] = useState<ScaffoldBody>(DEFAULT_FORM)
  const [preview, setPreview] = useState<ScaffoldPreview | null>(null)
  const [activeFile, setActiveFile] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  const { mutate: doPreview, isPending: previewing, error: previewError } = useScaffoldPreview()
  const { mutate: doCreate, isPending: creating, error: createError } = useScaffoldCreate()

  function updateForm(key: keyof ScaffoldBody, value: string | number | boolean) {
    setForm(f => ({ ...f, [key]: value }))
  }

  function handlePreview() {
    doPreview(form, {
      onSuccess: (data) => {
        setPreview(data)
        const firstKey = Object.keys(data)[0] ?? null
        setActiveFile(firstKey)
        setStep(2)
      },
    })
  }

  function handleCreate() {
    doCreate(form, {
      onSuccess: (data) => {
        setSuccessMsg(data.message)
        setStep(3)
      },
    })
  }

  const nameValid = /^[A-Z][A-Za-z0-9]{1,}$/.test(form.name)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
         style={{ background: 'oklch(0 0 0 / 60%)' }}>
      <div className="relative w-full max-w-3xl rounded-2xl shadow-2xl overflow-hidden flex flex-col"
           style={{ background: 'var(--surface-0)', maxHeight: '90vh' }}>

        <div className="flex items-center justify-between px-6 py-4"
             style={{ borderBottom: '1px solid var(--border)' }}>
          <div>
            <h2 className="text-lg font-semibold" style={{ color: 'var(--text-1)' }}>
              New Model
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-2)' }}>
              Step {step} of 3 — {step === 1 ? 'Configure' : step === 2 ? 'Preview files' : 'Done'}
            </p>
          </div>
          <button onClick={onClose} style={{ color: 'var(--text-2)' }}>
            <X size={18} />
          </button>
        </div>

        {step === 1 && (
          <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-1)' }}>
                Model Name <span style={{ color: 'oklch(0.66 0.22 25)' }}>*</span>
              </label>
              <input
                value={form.name}
                onChange={e => updateForm('name', e.target.value)}
                placeholder="e.g. DemoAD"
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border)',
                         color: 'var(--text-1)', outline: 'none' }}
              />
              <p className="text-xs mt-1" style={{ color: 'var(--text-2)' }}>
                PascalCase, no spaces (e.g. DemoAD, MyPredictor)
              </p>
              {form.name && !nameValid && (
                <p className="text-xs mt-1" style={{ color: 'oklch(0.66 0.22 25)' }}>
                  Must start with uppercase letter and contain only letters/digits
                </p>
              )}
            </div>

            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-1)' }}>Task</label>
              <select
                value={form.task}
                onChange={e => updateForm('task', e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm"
                style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-1)' }}
              >
                {TASKS.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </div>

            <div>
              <label className="block text-sm font-medium mb-2" style={{ color: 'var(--text-1)' }}>Task Type</label>
              <div className="flex gap-3">
                {TASK_TYPES.map(tt => (
                  <button
                    key={tt.value}
                    onClick={() => {
                      updateForm('task_type', tt.value)
                      if (tt.value === 'classification') {
                        updateForm('promotion_metric', 'accuracy')
                        updateForm('promotion_direction', 'higher_is_better')
                        updateForm('promotion_threshold', 0.85)
                      } else {
                        updateForm('promotion_metric', 'rmse')
                        updateForm('promotion_direction', 'lower_is_better')
                        updateForm('promotion_threshold', 100.0)
                      }
                    }}
                    className="flex-1 py-2 rounded-lg text-sm font-medium transition-all"
                    style={{
                      background: form.task_type === tt.value ? 'oklch(0.72 0.18 155 / 15%)' : 'var(--surface-1)',
                      border: form.task_type === tt.value ? '1px solid oklch(0.72 0.18 155 / 50%)' : '1px solid var(--border)',
                      color: form.task_type === tt.value ? 'oklch(0.72 0.18 155)' : 'var(--text-2)',
                    }}
                  >
                    {tt.label}
                  </button>
                ))}
              </div>
            </div>

            <details className="group">
              <summary className="cursor-pointer text-sm font-medium select-none"
                       style={{ color: 'var(--text-2)' }}>
                Advanced (promotion thresholds)
              </summary>
              <div className="mt-3 space-y-3 pl-3" style={{ borderLeft: '2px solid var(--border)' }}>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs mb-1" style={{ color: 'var(--text-2)' }}>Metric</label>
                    <input
                      value={form.promotion_metric}
                      onChange={e => updateForm('promotion_metric', e.target.value)}
                      className="w-full px-2 py-1.5 rounded text-sm"
                      style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-1)' }}
                    />
                  </div>
                  <div>
                    <label className="block text-xs mb-1" style={{ color: 'var(--text-2)' }}>Production threshold</label>
                    <input
                      type="number"
                      value={form.promotion_threshold}
                      onChange={e => updateForm('promotion_threshold', parseFloat(e.target.value))}
                      className="w-full px-2 py-1.5 rounded text-sm"
                      style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-1)' }}
                    />
                  </div>
                </div>
                <div>
                  <label className="block text-xs mb-1" style={{ color: 'var(--text-2)' }}>Direction</label>
                  <select
                    value={form.promotion_direction}
                    onChange={e => updateForm('promotion_direction', e.target.value)}
                    className="w-full px-2 py-1.5 rounded text-sm"
                    style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', color: 'var(--text-1)' }}
                  >
                    {DIRECTIONS.map(d => <option key={d.value} value={d.value}>{d.label}</option>)}
                  </select>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.force}
                    onChange={e => updateForm('force', e.target.checked)}
                  />
                  <span className="text-xs" style={{ color: 'var(--text-2)' }}>
                    Overwrite existing files (--force)
                  </span>
                </label>
              </div>
            </details>

            {previewError && (
              <p className="text-sm px-3 py-2 rounded-lg" style={{ background: 'oklch(0.66 0.22 25 / 10%)', color: 'oklch(0.66 0.22 25)' }}>
                {previewError.message}
              </p>
            )}
          </div>
        )}

        {step === 2 && preview && (
          <div className="flex flex-1 overflow-hidden">
            <div className="w-56 flex-shrink-0 overflow-y-auto py-3"
                 style={{ borderRight: '1px solid var(--border)', background: 'var(--surface-1)' }}>
              {Object.keys(preview).map(path => (
                <button
                  key={path}
                  onClick={() => setActiveFile(path)}
                  className="w-full text-left px-3 py-2 text-xs font-mono truncate"
                  style={{
                    color: activeFile === path ? 'oklch(0.72 0.18 155)' : 'var(--text-2)',
                    background: activeFile === path ? 'oklch(0.72 0.18 155 / 10%)' : 'transparent',
                  }}
                >
                  {path.split('/').pop()}
                  <span className="block text-xs opacity-60 truncate">{path}</span>
                </button>
              ))}
            </div>
            <div className="flex-1 overflow-auto">
              <pre className="p-4 text-xs font-mono leading-relaxed h-full overflow-auto"
                   style={{ color: 'var(--text-1)', background: 'var(--surface-0)', margin: 0 }}>
                {activeFile ? preview[activeFile] || '(empty)' : ''}
              </pre>
            </div>
          </div>
        )}

        {step === 3 && (
          <div className="flex-1 flex flex-col items-center justify-center p-8 gap-4 text-center">
            <CheckCircle2 size={40} style={{ color: 'oklch(0.72 0.18 155)' }} />
            <h3 className="text-lg font-semibold" style={{ color: 'var(--text-1)' }}>
              {form.name} scaffolded
            </h3>
            {successMsg && (
              <pre className="w-full text-left text-xs p-3 rounded-lg font-mono overflow-auto max-h-48"
                   style={{ background: 'var(--surface-1)', color: 'var(--text-2)' }}>
                {successMsg}
              </pre>
            )}
            <div className="text-sm rounded-xl p-4 w-full text-left space-y-1"
                 style={{ background: 'var(--surface-1)', border: '1px solid var(--border)' }}>
              <p className="font-medium flex items-center gap-2" style={{ color: 'var(--text-1)' }}>
                <Terminal size={14} /> Next steps
              </p>
              {[
                `Edit modelzoo/.../tasks/.../${form.name.toLowerCase()}/${form.name.toLowerCase()}_model.py`,
                `Edit pipelines/models/${form.name.toLowerCase()}.yaml — tune thresholds`,
                `exa pipeline validate`,
                `exa pipeline run --model ${form.name} --dataset FDataDataset --dummy`,
              ].map((s, i) => (
                <p key={i} className="text-xs font-mono pl-4" style={{ color: 'var(--text-2)' }}>
                  {i + 1}. {s}
                </p>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center justify-between px-6 py-4"
             style={{ borderTop: '1px solid var(--border)' }}>
          <button
            onClick={step === 1 ? onClose : () => setStep(s => (s - 1) as 1 | 2 | 3)}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm"
            style={{ color: 'var(--text-2)' }}
          >
            <ChevronLeft size={14} />
            {step === 1 ? 'Cancel' : 'Back'}
          </button>

          {step === 1 && (
            <button
              disabled={!nameValid || previewing}
              onClick={handlePreview}
              className="flex items-center gap-1.5 px-5 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
              style={{ background: 'oklch(0.72 0.18 155)', color: 'white' }}
            >
              {previewing ? <Loader2 size={14} className="animate-spin" /> : <ChevronRight size={14} />}
              Preview files
            </button>
          )}

          {step === 2 && (
            <button
              disabled={creating}
              onClick={handleCreate}
              className="flex items-center gap-1.5 px-5 py-2 rounded-lg text-sm font-medium disabled:opacity-50"
              style={{ background: 'oklch(0.72 0.18 155)', color: 'white' }}
            >
              {creating ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
              Create model
            </button>
          )}

          {step === 3 && (
            <button
              onClick={onClose}
              className="px-5 py-2 rounded-lg text-sm font-medium"
              style={{ background: 'oklch(0.72 0.18 155)', color: 'white' }}
            >
              Done
            </button>
          )}
        </div>

        {createError && step === 2 && (
          <div className="px-6 pb-4">
            <p className="text-sm px-3 py-2 rounded-lg"
               style={{ background: 'oklch(0.66 0.22 25 / 10%)', color: 'oklch(0.66 0.22 25)' }}>
              {createError.message}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
