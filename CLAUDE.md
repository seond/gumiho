We want to create an automated game client that plays a classic Multi-user Dungeon (MUD) game for players.

Here are a few requirements:

- The game is hosted in a remote server and connected through Telnet protocol.
- The content language is Korean encoded in euc-kr encoding.
- The command should be entered in a reasonable pace that simulates a skilled human player so it is efficient but not burden the system at the same time.
- The game follows a pattern of typical text MUD games
  - The player is placed in a room. The player can interact with objects, NPCs, enemies and other players in the room.
  - The player can move to other rooms through available direction.
  - Once a player enters a room, a description of the room will be given. Available directions and other entities that can be interacted with are also described.
    - If any of these is missed, all the description can be repeated by a command.
  - With the command prompt, some status numbers are given. Current health point, current magic point and current move point.
  - Many actions are within a single prompt. Which means, a command will finish the action and with the next prompt the player is free to take any other action.
    - But some actions initiate a long sequence of follow-ups, e.g. battle, and the prompts within the window will allow only a limited set of actions.
- The client should be driven by a local AI. MLX on Macbooks or Ollama on Windows machines.
  - The client should be able to learn how the game works on top of what is provided. It should accumulate its knowledge base.


A few thoughts about the implementation:
- Wonder if there are any good telnet library that can be used in either Golang, Python or Node.
- Or there's a specialized framework for interacting with text MUD game servers with custom logics.
- Responding to every turn might not be feasible for LLM because the pace is controlled by the server during battles. Probably could be a hybrid model-battles are handled by written script and overall control is handled by LLM.

Analyze the feasibility of this solution.