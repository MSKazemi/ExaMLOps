import MDEditor from '@uiw/react-md-editor'
import '@uiw/react-md-editor/markdown-editor.css'

interface MarkdownEditorProps {
  value: string
  onChange: (value: string) => void
}

/** Heavy markdown authoring surface, loaded only after an administrator enters edit mode. */
export function MarkdownEditor({ value, onChange }: MarkdownEditorProps) {
  return (
    <MDEditor
      value={value}
      onChange={(next) => onChange(next ?? '')}
      height={400}
      preview="live"
    />
  )
}
