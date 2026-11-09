/* Notepad — content that has to reflow on every resize frame.
 *
 * Not a singleton, on purpose: a second Notepad proves windows of one app are independent.
 */

export default {
  id: 'notepad',
  name: 'Untitled - Notepad',
  filetype: 'notes',
  onDesktop: true,
  width: 440,
  height: 300,
  status: true,
  styles: ['app.css'],

  mount({ win }) {
    const area = document.createElement('textarea');
    area.className = 'notepad-area';
    area.setAttribute('aria-label', 'Document text');
    area.spellcheck = false;
    area.value = 'Type here. Open a second Notepad from the Start menu and drag it off '
      + 'this one.\n\nThe text reflows on every resize frame.\n';

    const count = () => win.setStatus(`${area.value.length} characters`);
    area.addEventListener('input', () => {
      count();
      // The title tracks the document, and the taskbar button tracks the title. XP put the
      // asterisk nowhere, but it is the one thing the title bar is for.
      win.setTitle(area.value ? 'Document - Notepad' : 'Untitled - Notepad');
    });
    count();

    return area;
  },
};
