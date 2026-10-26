# Prompts I gave the AI assistant while building Tee:dentity

My prompts, verbatim and in order (typos kept). Notes in *italics* describe the context.

---

**1.**
> What's good man

**2.**
> Let's make an app.

**3.**
> 1-1) user choose its country. The country will become the very important source of 1-2) question generation.
> 1-2) user responses to 15 A/B option choosing questions, the A/B option questions are about what the user likes, so the app can define the user's personality and taste. After each response, the app will generate the next question. For the best diagnose of the user's character. The questions can be general to very specific quesions. For the concept of A/B option quetions, study this thoroughly (https://www.google.com/search?q=%EB%B0%B8%EB%9F%B0%EC%8A%A4%EA%B2%8C%EC%9E%84&oq=%EB%B0%B8%EB%9F%B0%EC%8A%A4%EA%B2%8C%EC%9E%84&gs_lcrp=EgZjaHJvbWUyBggAEEUYOTIHCAEQABiABDIHCAIQABiABDIHCAMQABiABDIHCAQQABiABDIHCAUQABiABDIHCAYQABiABDIHCAcQABiABDIHCAgQABiABDIHCAkQABiABNIBCDI1ODlqMGo3qAIAsAIA&sourceid=chrome&source=chrome.ob&ie=UTF-8). Each question should have simple UI, with same size image of each option
> 1-3) user can upload resume, essays, sns URL, such thinkgs that they want the app to know
> 2) App find and suggests t-shirts, that the user can buy, to express their identity/preference/taste/history
> 3) user can choose and buy the t-shirt
> 4) if user wants, the app can design the graphics, type of materials, fit, and many things to make a quick link for a personalized order

*(The search link is a Google search for 밸런스게임, "balance game".)*

**4.** *(sent while the assistant was working)*
> Ultracode. use it.

**5.**
> Try again

**6.**
> Try again

**7.**
> Man, finish it in 20 mins

**8.**
> Thanks. Do the following changes. Don't use more than 30 mins.
> 1) Shorten the questions into 10 questions, not 15 Qs.
> 2) Make the first page more straightforward, to show what this app does.
> 3) Change the questions into more cultural/food/vibe/idea/politics preferences. Make it fun. Make it hillarious. Maybe you can check Redit, Twitter, Instagram, such things to see what's fun

**9.**
> Cool give me the link

**10.**
> Thanks. Do the following changes. Don't use more than 30 mins.
> 1) Change the questions into more straightforward and easy to understand. Use the most popular/famous cultural codes (such as, for Japanese or Korean, Slamdunk vs Dragon Ball). Input more straightforward cultural/food/vibe/idea/politics preferences. Make it fun. Make it hillarious. Maybe you can check Redit, Twitter, Instagram, such things to see what's fun
> 2) I think the t-shirt options are not so related to the questions. Maybe you can edit the questions, to become more effectively informative to t-shirt suggestions. I mean, if you asked more simple and useful questions, maybe you could have give more strongly related t-shirt recommendations in the end.
> 3) aggregate file attachment window with the rest of the other input windows (such as comment window) to simplify the "about you" page

**11.**
> Don't use more than 25 mins, but fix these
> 1) Alright, still, t-shirt recommendation is not to relevant to the questions. Fix it. It's important. Maybe you can change the country options into five (US, KOR, JPN, China, and India) for efficient quality development. But state that there are more countries to be added to the app.
> 2) I think the "Find t-shirt" and "Design yours" icons of "Profile" should be on the upper side, so that use can immediately understand what the Profile page is.

**12.**
> Don't use more than 15 mins, but fix these
> 1) Make sure that "move to my profile" icon apperes at the top of the About you page
> 2) develop the questions and t-shirt suggestions more.

**13.**
> The app is now finished. Before we create the final report and slides, please prepare it for final-project submission.
>
> Use the MGT 409 final-project requirements as the checklist.
>
> Please do these things first:
>
> 1. Check the whole app and confirm that it works end to end with a real example.
> 2. Make sure the repo contains all code needed to run the app.
> 3. Check README.md and make it very clear, step by step, so a grader can run the app from a fresh clone within 10 minutes.
> 4. Make sure the app looks for .env in BOTH places:
>    - inside the project folder
>    - one folder above the project folder
> 5. Make sure .env.example lists every required key, but contains no real secrets.
> 6. Make sure the real .env, API keys, passwords, tokens, and other secrets are excluded from Git.
> 7. If I am using the course key, make sure it uses PORTKEY_API_KEY and only gpt-6-luna.
> 8. Check all file paths so the app still works after being cloned to a new folder.
> 9. Identify one good real run that we can use as evidence in the report.
> 10. Identify where the app records token usage, cached input tokens, output tokens, and cost. If it does not record these yet, tell me before changing anything.
> 11. Identify one meaningful safety risk for this app and whether we already have a defense that we can actually test and screenshot.
> 12. Tell me what screenshots/evidence I still need to collect for:
>     - agent workflow
>     - tokenomics
>     - safety
>     - impact/user testing
>
> Do not create report.html or slides.html yet.
> Do not invent any results, costs, metrics, user feedback, or screenshots.
>
> First give me a short checklist of:
> - READY
> - NEEDS FIX
> - EVIDENCE STILL NEEDED

**14.** *(sent while the assistant was working)*
> Man, this is the requirement
> https://zlisto.github.io/mgt_409_fa26/project_requirements.html

**15.**
> Alright then, I'll just use Luna.
> Then, give me prompts that I gave you, I'll share the prompts to Luna so that it can learn from it.

---

## Historical handoff notes (superseded by the final audit, Oct 26)

The checklist that previously followed this heading was an earlier snapshot and no longer describes the current source. In the final project state, every model role is fixed to `gpt-6-luna`; configuration checks the backend folder, project folder, one folder above, and a legacy shared path; `.env.example` is present with placeholder values; token usage is recorded in SQLite; the 10-question live smoke passed 24/24 steps; and the safety evidence is listed under `evidence/`. The 37-request smoke measurement and later 43-request session total have different boundaries, as explained in the smoke evidence record.
