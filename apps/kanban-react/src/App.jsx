import AddColumnForm from './components/AddColumnForm';
import AddTicketForm from './components/AddTicketForm';
import KanbanBoard from './components/KanbanBoard';
import useBoard from './hooks/useBoard';

export default function App() {
  const board = useBoard();

  return <main className="app">
    <header className="page-header">
      <p className="eyebrow">YOUR WORK, IN VIEW</p>
      <h1>Kanban board</h1>
      <p>Make a little progress, one ticket at a time.</p>
    </header>
    <div className="toolbar">
      <AddTicketForm columns={board.columns} addTicket={board.addTicket} />
      <AddColumnForm addColumn={board.addColumn} columnTitleError={board.columnTitleError} />
    </div>
    <KanbanBoard board={board} />
  </main>;
}
