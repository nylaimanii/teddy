/* "Teddy is sleeping" demo: plays the same screens the real bear sends, spoken by the browser.
   Used only when the Mac's brain can't be reached. Every line here mirrors what brain/agent.py says. */
window.Demo = (() => {
  let run = 0;  // bumping this cancels whatever scene is playing
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const say = text => new Promise(resolve => {
    onSay({ id: 'demo', text, resolve });
    addEvent({ id: 'd' + Math.random(), kind: 'speak', data: { reply: text } });
  });
  const show = m => render({ type: 'screen', ...m });

  const STORY = { title: 'Teddy and the Sleepy Moon', pages: [
    { text: 'Once upon a time, a little teddy bear looked up and saw the moon yawning.', art: '🧸🌙😴', scene: 'night' },
    { text: "Why are you so sleepy, Moon? asked Teddy. I've been shining all night, said the Moon.", art: '🌙✨⭐', scene: 'night' },
    { text: 'So Teddy hummed a soft song, and all the stars twinkled along.', art: '🧸🎵⭐⭐', scene: 'space' },
    { text: 'The Moon smiled and closed its eyes, and the Sun peeked up to say good morning.', art: '🌙😊🌅', scene: 'sunset' },
    { text: 'And Teddy curled up for a cozy nap, happy that he helped a friend.', art: '🧸💤💛', scene: 'meadow' },
  ] };
  const STEPS = [
    { show: '7 × 8', say: "Let's find 7 times 8. That means 7 groups of 8." },
    { show: '7 × 8 = 8 + 8 + 8 + 8 + 8 + 8 + 8', say: 'We can add eight, seven times.' },
    { show: '8 + 8 = 16 → 24 → 32 → 40 → 48 → 56', say: 'Counting up by eights: 16, 24, 32, 40, 48, 56.' },
    { show: '7 × 8 = 56', say: 'So 7 times 8 is 56!' },
  ];

  const SCENES = {
    async find_object(r) {
      show({ mode: 'find', object: 'remote', status: 'looking', caption: 'Looking for your remote…' });
      await say("Let me look for your remote!"); if (r !== run) return;
      show({ mode: 'find', object: 'remote', status: 'found', frame: '/demo/find-remote.jpg', when: 'just now',
             caption: 'Your remote is on the left!' });
      await say('Your remote is on the left! I raised my arm to point right at it.');
    },
    async story(r) {
      show({ mode: 'think', caption: 'Thinking of a story…' });
      await say('Ooh, a story! Let me think of a good one.');
      for (let i = 0; i < STORY.pages.length; i++) {
        if (r !== run) return;
        show({ mode: 'story', ...STORY, page: i });
        await say(STORY.pages[i].text);
      }
      if (r !== run) return;
      show({ mode: 'story', ...STORY, page: STORY.pages.length - 1, done: true });
      await say('The end! Did you like it?');
    },
    async homework(r) {
      show({ mode: 'think', caption: 'Let me work it out…' });
      await say("Let's work it out together!");
      for (let i = 0; i < STEPS.length; i++) {
        if (r !== run) return;
        show({ mode: 'homework', question: 'What is 7 times 8?', steps: STEPS, step: i, answer: '56' });
        await say(STEPS[i].say);
      }
      if (r !== run) return;
      show({ mode: 'homework', question: 'What is 7 times 8?', steps: STEPS, step: STEPS.length - 1, answer: '56', done: true });
      await say("You're so smart! Want to try another one?");
    },
    async read() {
      show({ mode: 'think', caption: 'Hold it up for me…' });
      await say("Hold it up for me, I'll read it.");
      const text = "Dear Rose, happy birthday! We love you and we'll visit on Sunday. Love, Maya.";
      show({ mode: 'read', text });
      await say(text);
    },
    async dance(r) {
      show({ mode: 'dance' });
      await say("Dance party! Let's go!"); await wait(4000); if (r !== run) return;
      await say('Whew! That was fun!'); show({ mode: 'idle' });
    },
    async cpr_coach(r) {
      show({ mode: 'cpr', bpm: 110, caption: 'Call 911 now' });
      await say('Call 911 now, and put them on speaker. Put the heel of your hand in the middle of their chest. Push hard and fast with my arms.');
      if (r !== run) return;
      show({ mode: 'cpr', bpm: 110, round: 1, caption: 'Push hard and fast' });
      await say("In real life I keep the beat with my arms until help arrives. Tap Stop when you're done.");
    },
    async fall_check() {
      show({ mode: 'check', status: 'checking', caption: 'Are you okay?' });
      await say("I'm checking on you. Are you feeling okay? You can tap a button to answer me.");
    },
    async mood_checkin() {
      show({ mode: 'mood', caption: 'How are you feeling today?' });
      await say('How are you feeling today?');
    },
    async identify() {
      show({ mode: 'identify', thing: 'a red toy car' });
      await say("Ooh! I think that's a red toy car.");
    },
    async stop() { hush(); show({ mode: 'idle' }); await say('Okay, stopping.'); },
  };

  function play(intent) {
    run++; hush();
    return (SCENES[intent] || SCENES.identify)(run);
  }

  function tell(text) {
    const t = text.toLowerCase();
    addEvent({ id: 'd' + Math.random(), kind: 'heard', data: { text } });
    if (/okay|fine|good|great|yes/.test(t) && ['check', 'mood'].includes(S.mode)) {
      run++; hush();
      show({ mode: 'check', status: 'ok', caption: "Glad you're okay!" });
      return say("Phew! I'm glad you're okay. I'm right here if you need me.");
    }
    if (/sad|lonely|help/.test(t)) {
      run++; hush();
      return say("I'm here with you. In real life I'd let your family know, and they'd see it on their dashboard.");
    }
    if (/\d|times|plus|homework/.test(t)) return play('homework');
    if (/story/.test(t)) return play('story');
    if (/find|where/.test(t)) return play('find_object');
    if (/danc/.test(t)) return play('dance');
    run++; hush();
    return say("I'm sleeping right now, so I can only do pretend demos. Try asking for a story, or what 7 times 8 is!");
  }

  return { play, tell, say };
})();
