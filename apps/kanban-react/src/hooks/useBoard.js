import { useEffect, useState } from 'react';

const storageKey = 'kanban-board';
const defaultBoard = () => ({
  columns: ['To Do', 'In Progress', 'Done'].map((title, index) => ({ id: `column-${index}`, title })),
  tickets: [],
});

function isValidBoard(board) {
  if (!board || !Array.isArray(board.columns) || !Array.isArray(board.tickets)) return false;
  const ids = new Set();
  const titles = new Set();
  for (const column of board.columns) {
    if (!column || typeof column.id !== 'string' || !column.id ||
      typeof column.title !== 'string' || !column.title.trim() ||
      ids.has(column.id) || titles.has(column.title.trim().toLowerCase())) return false;
    ids.add(column.id);
    titles.add(column.title.trim().toLowerCase());
  }
  const ticketIds = new Set();
  return board.tickets.every((ticket) => {
    if (!ticket || typeof ticket.id !== 'string' || !ticket.id || ticketIds.has(ticket.id) ||
      typeof ticket.title !== 'string' || !ticket.title.trim() || !ids.has(ticket.columnId) ||
      (ticket.description !== undefined && typeof ticket.description !== 'string')) return false;
    ticketIds.add(ticket.id);
    return true;
  });
}

function loadBoard() {
  try {
    const board = JSON.parse(localStorage.getItem(storageKey));
    return isValidBoard(board) ? board : defaultBoard();
  } catch {
    return defaultBoard();
  }
}

export default function useBoard() {
  const [board, setBoard] = useState(loadBoard);

  useEffect(() => {
    localStorage.setItem(storageKey, JSON.stringify(board));
  }, [board]);

  function columnTitleError(title, ownId) {
    if (!title.trim()) return 'Column title is required.';
    return board.columns.some((column) => column.id !== ownId &&
      column.title.trim().toLowerCase() === title.trim().toLowerCase())
      ? 'A column with that name already exists.' : '';
  }

  function addColumn(title) {
    const column = { id: crypto.randomUUID(), title: title.trim() };
    setBoard((prev) => ({ ...prev, columns: [...prev.columns, column] }));
  }

  function renameColumn(id, title) {
    setBoard((prev) => ({ ...prev, columns: prev.columns.map((column) =>
      column.id === id ? { ...column, title: title.trim() } : column) }));
  }

  function deleteColumn(id) {
    setBoard((prev) => prev.tickets.some((ticket) => ticket.columnId === id) ? prev : {
      ...prev, columns: prev.columns.filter((column) => column.id !== id),
    });
  }

  function addTicket(title, columnId) {
    const ticket = { id: crypto.randomUUID(), title: title.trim(), description: '', columnId };
    setBoard((prev) => !ticket.title || !prev.columns.some((column) => column.id === columnId)
      ? prev : { ...prev, tickets: [...prev.tickets, ticket] });
  }

  function updateTicket(id, changes) {
    setBoard((prev) => ({ ...prev, tickets: prev.tickets.map((ticket) =>
      ticket.id === id ? { ...ticket, ...changes } : ticket) }));
  }

  function moveTicket(id, columnId) {
    setBoard((prev) => {
      if (!prev.columns.some((column) => column.id === columnId) ||
        !prev.tickets.some((ticket) => ticket.id === id && ticket.columnId !== columnId)) return prev;
      return { ...prev, tickets: prev.tickets.map((ticket) =>
        ticket.id === id ? { ...ticket, columnId } : ticket) };
    });
  }

  function deleteTicket(id) {
    setBoard((prev) => ({ ...prev, tickets: prev.tickets.filter((ticket) => ticket.id !== id) }));
  }

  return { ...board, columnTitleError, addColumn, renameColumn, deleteColumn,
    addTicket, updateTicket, moveTicket, deleteTicket };
}
