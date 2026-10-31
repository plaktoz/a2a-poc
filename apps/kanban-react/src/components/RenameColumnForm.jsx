import { useId, useState } from 'react';

export default function RenameColumnForm({ column, columnTitleError, onSave, onCancel }) {
  const id = useId();
  const [title, setTitle] = useState(column.title);
  const [error, setError] = useState('');

  function submit(event) {
    event.preventDefault();
    const message = columnTitleError(title, column.id);
    setError(() => message);
    if (!message) onSave(title);
  }

  return <form className="inline-form" onSubmit={submit}>
    <div className="field"><label htmlFor={id}>Column title</label>
      <input autoFocus id={id} value={title} onChange={(event) => setTitle(() => event.target.value)} />
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="actions"><button type="submit">Save column</button><button className="secondary" type="button" onClick={onCancel}>Cancel</button></div>
  </form>;
}
