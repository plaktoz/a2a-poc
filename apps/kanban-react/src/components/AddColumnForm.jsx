import { useId, useState } from 'react';

export default function AddColumnForm({ addColumn, columnTitleError }) {
  const id = useId();
  const [name, setName] = useState('');
  const [error, setError] = useState('');

  function submit(event) {
    event.preventDefault();
    if (!name.trim()) return;
    const message = columnTitleError(name);
    setError(() => message);
    if (message) return;
    addColumn(name);
    setName(() => '');
  }

  return <form className="creation-form" onSubmit={submit}>
    <div className="field"><label htmlFor={id}>Column name</label>
      <input id={id} value={name} onChange={(event) => setName(() => event.target.value)} placeholder="e.g. Review" />
    </div>
    <button type="submit" className="secondary">Add column</button>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}
