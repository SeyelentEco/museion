// Switching tabs. Each tab's file says what to refresh when it opens, with
// onTab('library', refresh), so this file needs to know none of them.

import { $ } from './dom.js';

const openers = {};

export function onTab(name, fn) {
  (openers[name] ??= []).push(fn);
}

export function showTab(name) {
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-current', String(t.dataset.tab === name)));
  document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
  $(`#panel-${name}`).classList.add('active');
  for (const fn of openers[name] ?? []) void fn();
}

/**
 * First-run set-up is its own screen, shown when there is nothing else to
 * show. Once the machine is set up it goes away; a link in Settings brings it
 * back for a new machine or a fresh start.
 */
export function showWelcome() {
  $('header nav').setAttribute('hidden', '');
  showTab('welcome');
}

export function leaveWelcome(to) {
  $('header nav').removeAttribute('hidden');
  showTab(to);
}

document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
