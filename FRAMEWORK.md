Let's consider all the experiences so far as a research phase, and let's solidify the engine now.

After trying a lot of different things in a very free form, I've come to a realization that we need for structured instructions rather than trying to openly define the logic. Rather than decide what to do next from scratch relying on LLM, we need a bigger framework of driving localized workflows.

A few findings from the observations so far:
- Involving LLM in a real-time decision does not work well. It is often too sluggish, or inaccurate. Most of the reactions should be handled by predefined reflexes with almost no delay at all.

The elements of the frameworks, to start off with:
- A: Information structure that stores all useful current states and long-term knowledges about the zones, items, mobs, etc.
- B: Task driver that decides the next move based on the current states.
- C: Dynamically packaged bundles of reflexes.
- D: Layer that organizes B and C by the current context of the character based on the purpose, workflow, etc.
- E: Tools that are designed to serve specialized requests
  - Map + Navigation
- F: Local LLM serves the needs from any of D, E for understanding of the knowledge base. For example, what are the right portions to stock and use in a character while hunting. What is the right zones to hunt in. What is the historic damage range that mob A has caused.
- G: UI that embeds the main game consoles and all supporting state displays and widgets.
  - Entity viewers for stored items
  - Local map viewer

# Fundamental Challenges
I had tried automations on this MUD multiple times pre-LLM and it was never really successful for some reasons:
- Stochastic events that requires retries at a proper rate.

# Workflows
We need a clearly defined logic flow charts for the workflows we have to automate.

For now, setting up the characters such as equipping the characters with the right gears, training for spells will be handled manually by human.

The primary activity we'd like to automate is hunting for experience points to level up the characters.

## Hunting
The core of hunting is:
1. To get to a place where suitable mobs are plentiful. Definition of being suitable is the balance between the difficulty/risk of fighting against a mob and the reward which is the experience point it gives with the killing.
2. To clear up the mobs in the rooms, one by one.

Some off-cycle behaviors that are essential to hunting activity:
- Flee when the HP is dangerously low.
  - If supporter is also in the battle, the supporter should also flee.
  - The characters should regroup and take a rest to recover and resume hunting.
- Maintain the healthy status of both the leader and the supporter.
  - Proper portion should be taken by each character. If portion runs out, the characters have to rest through the sleeping process ("자" until the character is fully recovered, "깨" and "일" upon fully recovered)
  - Leader's HP needs to be maintained.
  - Supporter's needs to be maintained.
  - If the characters are at a dangerous room with the mobs that attacks first, the characters should move to a non-dangerous room first to take a rest.
- The leader should maintain all required buffs by requesting spells to the supporter in the room.
  - First, in the beginning of a hunting session, it should be assumed that the leader does not have any buff casted and request for all required spells.
  - Later, every while, the leader should recognize the expiries of spells and request the expired spell again. The supporter should respond to the request and cast the spell.
    - Every spell has different expiry so the casted spell should be tracked closely in the leader character's current states.
- Stocking the supplies
 - For the leader
   - HP portions for 전사/검사/장군 class: the supporter class for these classes is one of 마법사/흑마법사/마왕 and these classes do not have spell to recover HP. HP Portion is needed for that reason.
   - 시루떡 from 한성 떡집 for resolving the hunger
   - Refilling 버드와이저 with water from 분수 in 광장 사거리 for resolving thirst
 - For the supporter
   - MP portions
   - 시루떡 from 한성 떡집 for resolving the hunger
   - Refilling 버드와이저 with water from 분수 in 광장 사거리 for resolving thirst
 - Repairing gears: gears can be lost when the endurance goes to 0. They can be repaired at 대장간 in 한성. Reparing can be bundled with restocking 시루떡 and refilling 버드와이저.
- In case all the mobs all cleared out, wait for the regeneration of the mobs. Usually takes 20m or longer.
- If any character dies, the dead character must finish 시체수습 process ("시체수습" -> wearing all gears including the weapon, light source and the "hold"s -> 시체 묻어 to recover hp/mp -> "보험" for insurance) and regroup + reset for a new hunting session.



# More specifics

## A: Information layer

Entities needed for A:
- Mobs
- Portions

## E: Special tools

### Map + Navigation

Two roles: inter-zone travels and in-zone traverses.

For the inter-zone travels, the character should be able to navigate to anywhere in the entire world based on the structured information in the database. But it may not be possible right at the moment. So we will use a predefined series of action, from the 중앙 광장 anchor to get there. If the predefined entry does not exist, it should ask for human input through recording the actions starting from 중앙 광장. Namely, the character is placed at 중앙 광장, recording starts, human takes the character to the destination, and the recording ends with being stored in the storage with the name of destination which should represent a zone most of the time.

For the in-zone traverses, it should recognize the rooms in the hunting zone during a session and organize the hunting path that can cover the entire zone efficiently without randomly straying.

## G: UI

### Entity viewers
The entities (zones, mobs, portions, etc.) should be listed in a view that can be pulled up by buttons.
The views should be able to take inputs from the user where needed.

### Local map viewer
Should display local map and should take inputs where to hunt at or not.