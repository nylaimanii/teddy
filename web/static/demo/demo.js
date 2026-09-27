/* "Teddy is sleeping" demo: the same screens the real bear sends, played in the browser.
   Mirrors brain/agent.py: find, how-to coach (waits for Done), homework tutor (never gives the answer), read, badge. */
window.Demo = (() => {
  let run = 0, waiter = null, guides = null;
  const say = (text, mood) => new Promise(resolve => onSay({ id: 'demo', text, mood, resolve }));
  const show = m => render({ type: 'screen', ...m });
  const answer = () => new Promise(r => { waiter = r; });            // next tap/typed answer
  const load = async () => guides || (guides = await (await fetch('/demo/guides.json')).json());

  async function find(r, obj = 'remote') {
    show({ mode: 'find', object: obj, status: 'looking', face: 'thinking' });
    await say(`Let me look for your ${obj}!`); if (r !== run) return;
    show({ mode: 'find', object: obj, status: 'found', frame: '/demo/find-remote.jpg', when: 'just now', face: 'happy',
           caption: `Your ${obj} is on the left!` });
    await say(obj === 'remote' ? 'Your remote is on the left! See my arm pointing?' : `In the demo I can only find the remote, but on the real bear I'd look for your ${obj}!`);
  }

  async function howto(r, id = 'tie-shoes') {
    const g = (await load()).find(x => x.id === id) || guides[0];
    const card = { guide: g, steps: g.steps.map(s => ({ show: s.show, draw: s.draw || '' })), total: g.steps.length };
    await say(`Ooh, let's ${g.title.toLowerCase()}! Tap Done when you finish each step.`, 'happy');
    for (let i = 0; i < g.steps.length; i++) {
      if (r !== run) return;
      show({ mode: 'howto', step: i, face: 'talking', ...card });
      await say(g.steps[i].say);
      for (;;) {
        if (r !== run) return;
        show({ mode: 'howto', step: i, face: 'listening', waiting: true, ...card });
        const a = (await answer()).toLowerCase(); if (r !== run) return;
        if (/done|next|yes|ok/.test(a)) { if (i < g.steps.length - 1) await say(['Ooh nice!', 'You got it!', 'Look at you go!'][i % 3], 'happy'); break; }
        if (/help/.test(a)) await say(g.steps[i].tip || 'You got this!');
        else await say(g.steps[i].say);
      }
    }
    if (r !== run) return;
    show({ mode: 'badge', badge: g.badge, icon: g.icon, status: 'saved', face: 'happy' });
    await say(`You did it! You earned the ${g.badge} badge! On the real bear it goes on the Solana blockchain.`, 'happy');
  }

  async function homework(r) {
    const steps = [{ show: '5 × 8 = ?', ask: "7 times 8 is 7 groups of 8. First, what's 5 times 8?", hint: 'Count by 8s five times!', v: '40' },
                   { show: '2 × 8 = ?', ask: "Now the other 2 groups. What's 2 times 8?", hint: 'Count by 8s, two times.', v: '16' },
                   { show: '40 + 16 = ?', ask: "Put them together! What's 40 plus 16?", hint: 'Start at 40 and add 16.', v: '56' }];
    const solved = [], base = { question: 'What is 7 times 8?', problem: '7 × 8', steps: steps.map(s => ({ show: s.show })) };
    await say("Ooh, let's figure it out together!");
    for (let i = 0; i < steps.length; i++) {
      if (r !== run) return;
      show({ mode: 'homework', step: i, solved, face: 'talking', ...base });
      await say(steps[i].ask);
      for (let tries = 0; ; tries++) {
        if (r !== run) return;
        const a = await answer(); if (r !== run) return;
        if (/help|hint/i.test(a)) { await say(steps[i].hint); tries--; continue; }
        if (a.replace(/\D/g, '') === steps[i].v) { solved.push({ i, value: steps[i].v }); show({ mode: 'homework', step: i, solved, face: 'happy', ...base });
          if (i < steps.length - 1) await say('Yes! Ooh nice.', 'happy'); break; }
        await say(tries === 0 ? `Hmm, not quite! ${steps[i].hint}` : 'So close! Try counting it out slowly.');
      }
    }
    if (r !== run) return;
    show({ mode: 'homework', step: 2, solved, done: true, face: 'happy', ...base });
    await say('You figured it out yourself! 7 times 8 is 56. Nice work!', 'happy');
  }

  async function read() {
    const text = 'The elephant was very hungry. Let\'s sound out the tricky words. e-le-phant. Elephant!';
    show({ mode: 'think', caption: 'Hold it up for me…', face: 'thinking' });
    await say("Hold it up for me, I'll read it.");
    show({ mode: 'read', text, face: 'talking' });
    await say(text, 'reading');
  }

  const SCENES = { find_object: (r, x) => find(r, x.object), howto: (r, x) => howto(r, x.guide_id), homework, read,
                   stop: async () => { hush(); show({ mode: 'idle' }); await say('Okay! We can do it later.'); } };

  function play(intent, extra = {}) {
    run++; waiter = null; hush();
    return (SCENES[intent] || (async () => say("In the demo I can find things, teach how-tos, help with homework, and read!")))(run, extra);
  }
  function tell(text) {
    if (waiter) { const w = waiter; waiter = null; w(text); return; }
    const t = text.toLowerCase();
    if (/\d|times|plus|homework/.test(t)) return play('homework');
    if (/how|teach|show/.test(t)) return play('howto', {});
    if (/find|where|lost/.test(t)) return play('find_object', { object: 'remote' });
    return play('none');
  }
  return { play, tell, say };
})();
