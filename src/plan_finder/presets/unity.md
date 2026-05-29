# Unity Client

## Description
Unity engine client project (mobile, PC, or console game)

## Tags
unity, unity3d, game, client, csharp, c#, 유니티, 게임, 클라이언트

## Prompt
Find one area for improvement in this Unity client project. You must analyze only the C# files.

Key areas to analyze:
- **GC Allocation**: Heap allocation in Update loops, unnecessary string concatenation, excessive use of LINQ, misuse of `new`
- **Readability**: Incorrect naming of variables, methods, delegates, classes, structs, interfaces, comments, and namespaces; difficult-to-read code; failure to adhere to team code conventions
- **Poor Architecture**: Incorrect DI, duplicate implementations, non-use of design patterns
- **Logic Flaws**: Poorly implemented code logic, incorrect ordering of ECS systems, code with NRE risks
- **MonoBehaviour Misuse**: Empty Update/Awake/Start methods, frame-by-frame GetComponent calls, abuse of FindObjectOfType
- **Memory Management**: Unreleased Asset references, unreleased unmanaged objects, memory leaks during scene transitions, excessive use of Resources.Load
- **Coroutine/Asynchronous Mix**: Inconsistent mixing of coroutines and async/await

Exclude the following from analysis:
- Assets/ThirdParty/, Assets/Plugins/ folders
- .meta, .asset, .unity, .prefab files (do not attempt to read these)
- Library/, Temp/, Logs/ folders