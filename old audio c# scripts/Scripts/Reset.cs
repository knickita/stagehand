using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Reset : MonoBehaviour
{
    UIManager uiManager;
    // Start is called before the first frame update
    void Start()
    {
        uiManager = FindObjectOfType<UIManager>();
    }
    
    public void ResetSimulation(){
        uiManager.DeselectAll();
        foreach (Selectable se in GameObject.FindObjectsOfType<Selectable>()){
            uiManager.AddToSelection(se.gameObject);
        }
        uiManager.RemoveSelected();
        Camera.main.transform.position=new Vector3(0,500,0);
    }
}
