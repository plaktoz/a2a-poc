import { useId, useState } from 'react';

export default function TicketEditForm({ ticket, onSave, onCancel }) {
  const id = useId();
  const [title, setTitle] = useState(ticket.title);
  const [description, setDescription] = useState(ticket.description ?? '');
  const [error, setError] = useState('');

  function submit(event) {
    event.preventDefault();
    if (!title.trim()) {
      setError(() => 'Title is required.');
      return;
    }
    onSave({ title: title.trim(), description: description.trim() });
  }

  return <form className="inline-form" onSubmit={submit}>
    <div className="field"><label htmlFor={`${id}-title`}>Title</label>
      <input autoFocus id={`${id}-title`} value={title} onChange={(event) => setTitle(() => event.target.value)} />
    </div>
    <div className="field"><label htmlFor={`${id}-description`}>Description</label>
      <textarea id={`${id}-description`} rows={4} value={description} onChange={(event) => setDescription(() => event.target.value)} />
    </div>
    {error && <p role="alert" className="error">{error}</p>}
    <div className="actions"><button type="submit">Save ticket</button><button type="button" className="secondary" onClick={onCancel}>Cancel</button></div>
  </form>;
}
